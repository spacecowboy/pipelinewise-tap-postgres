import copy
import time
import psycopg2
import psycopg2.extras
import singer

from singer import utils
from functools import partial
from singer import metrics

import tap_postgres.db as post_db


LOGGER = singer.get_logger('tap_postgres')

UPDATE_BOOKMARK_PERIOD = 10000


# pylint: disable=invalid-name,missing-function-docstring
def fetch_max_replication_key(conn_config, replication_key, schema_name, table_name):
    with post_db.open_connection(conn_config, False) as conn:
        with conn.cursor() as cur:
            max_key_sql = """SELECT max({})
                              FROM {}""".format(post_db.prepare_columns_sql(replication_key),
                                                post_db.fully_qualified_table_name(schema_name, table_name))
            LOGGER.debug("determine max replication key value: %s", max_key_sql)
            cur.execute(max_key_sql)
            max_key = cur.fetchone()[0]
            LOGGER.debug("max replication key value: %s", max_key)
            return max_key


# pylint: disable=too-many-locals,too-many-statements
def sync_table(conn_info, stream, state, desired_columns, md_map):
    time_extracted = utils.now()

    stream_version = singer.get_bookmark(state, stream['tap_stream_id'], 'version')
    if stream_version is None:
        stream_version = int(time.time() * 1000)

    state = singer.write_bookmark(state,
                                  stream['tap_stream_id'],
                                  'version',
                                  stream_version)
    singer.write_message(singer.StateMessage(value=copy.deepcopy(state)))

    schema_name = md_map.get(()).get('schema-name')

    escaped_columns = map(partial(post_db.prepare_columns_for_select_sql, md_map=md_map), desired_columns)

    activate_version_message = singer.ActivateVersionMessage(
        stream=post_db.calculate_destination_stream_name(stream, md_map),
        version=stream_version)


    singer.write_message(activate_version_message)

    replication_key = md_map.get((), {}).get('replication-key')
    if not replication_key:
        raise ValueError(f"No replication key present in {stream['table_name']} but that is required for INCREMENTAL sync")
    replication_key_value = singer.get_bookmark(state, stream['tap_stream_id'], 'replication_key_value')
    replication_key_prop = md_map.get(('properties', replication_key))
    if not replication_key_prop:
        raise ValueError(f"No type information on replication key in {stream['table_name']}")
    replication_key_sql_datatype = replication_key_prop.get('sql-datatype')

    hstore_available = post_db.hstore_available(conn_info)
    with metrics.record_counter(None) as counter:
        with post_db.open_connection(conn_info) as conn:

            # Client side character encoding defaults to the value in postgresql.conf under client_encoding.
            # The server / db can also have its own configred encoding.
            with conn.cursor() as cur:
                cur.execute("show server_encoding")
                LOGGER.debug("Current Server Encoding: %s", cur.fetchone()[0])
                cur.execute("show client_encoding")
                LOGGER.debug("Current Client Encoding: %s", cur.fetchone()[0])

            if hstore_available:
                LOGGER.debug("hstore is available")
                psycopg2.extras.register_hstore(conn)
            else:
                LOGGER.debug("hstore is Unavailable")

            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor, name='pipelinewise') as cur:
                cur.itersize = post_db.CURSOR_ITER_SIZE
                LOGGER.info("Beginning new incremental replication sync %s", stream_version)
                if replication_key_value:
                    select_sql = """SELECT {}
                                    FROM {}
                                    WHERE {} >= '{}'::{}
                                    ORDER BY {} ASC""".format(','.join(escaped_columns),
                                                              post_db.fully_qualified_table_name(schema_name,
                                                                                                 stream['table_name']),
                                                              post_db.prepare_columns_sql(replication_key),
                                                              replication_key_value,
                                                              replication_key_sql_datatype,
                                                              post_db.prepare_columns_sql(replication_key))
                else:
                    #if not replication_key_value
                    select_sql = """SELECT {}
                                    FROM {}
                                    ORDER BY {} ASC""".format(','.join(escaped_columns),
                                                              post_db.fully_qualified_table_name(schema_name,
                                                                                                 stream['table_name']),
                                                              post_db.prepare_columns_sql(replication_key))

                LOGGER.info('select statement: %s with itersize %s', select_sql, cur.itersize)
                cur.execute(select_sql)

                rows_saved = 0

                for rec in cur:
                    record_message = post_db.selected_row_to_singer_message(stream,
                                                                            rec,
                                                                            stream_version,
                                                                            desired_columns,
                                                                            time_extracted,
                                                                            md_map)

                    singer.write_message(record_message)
                    rows_saved = rows_saved + 1

                    # Picking a replication_key with NULL values will result in it ALWAYS been synced which is not great
                    # even worse would be allowing the NULL value to enter into the state
                    try:
                        if record_message.record[replication_key] is not None:
                            state = singer.write_bookmark(state,
                                                          stream['tap_stream_id'],
                                                          'replication_key_value',
                                                          record_message.record[replication_key])
                    except KeyError:
                        # Replication key not present in table - treat like None
                        LOGGER.info("Replication key was NULL in %s", stream['table_name'])

                    if rows_saved % UPDATE_BOOKMARK_PERIOD == 0:
                        singer.write_message(singer.StateMessage(value=copy.deepcopy(state)))

                    counter.increment()

    return state
