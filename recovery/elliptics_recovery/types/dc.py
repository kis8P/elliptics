"""
XXX:
"""

import sys
import logging as log

from itertools import groupby
from multiprocessing import Pool

from ..iterator import Iterator, IteratorResult
from ..time import Time
from ..stat import Stats
from ..utils.misc import elliptics_create_node, elliptics_create_session, worker_init

# XXX: change me before BETA
sys.path.insert(0, "bindings/python/")
import elliptics

log.getLogger()


def run_iterators(ctx, range, stats):
    """
    Runs local and remote iterators for each range.
    TODO: We can group iterators by host and run them in parallel
    TODO: We can run only one iterator per host if we'll teach iterators to "batch" all key ranges in one request
    """
    node = elliptics_create_node(address=ctx.address, elog=ctx.elog)

    try:
        timestamp_range = ctx.timestamp.to_etime(), Time.time_max().to_etime()

        local_eid = range.address[ctx.group_id][0]

        log.debug("Running local iterator on: {0} on node: {1}".format(range.id_range, range.address[ctx.group_id][1]))
        local_result = Iterator(node, ctx.group_id).start(eid=local_eid,
                                                          timestamp_range=timestamp_range,
                                                          key_ranges=[range.id_range],
                                                          tmp_dir=ctx.tmp_dir,
                                                          address=ctx.address
                                                          )

        local_result_len = len(local_result)
        stats.counter.local_records += local_result_len
        ctx.monitor.add_counter("local_records", local_result_len)
        stats.counter.iterated_keys += local_result_len
        ctx.monitor.add_counter("iterated_keys", local_result_len)
        stats.counter.iterations += 1
        ctx.monitor.add_counter("iterations", 1)
        log.debug("Local iterator obtained: {0} record(s)".format(len(local_result)))
        remote_result = []

        for i in range.address:
            if i == ctx.group_id:
                continue
            remote_eid = range.address[i][0]

            log.debug("Running remote iterator on:{0} on node: {1}".format(range.id_range, range.address[i][1]))

            it_result = Iterator(node, i).start(eid=remote_eid,
                                                timestamp_range=timestamp_range,
                                                key_ranges=[range.id_range],
                                                tmp_dir=ctx.tmp_dir,
                                                address=range.address[i][1]
                                                )

            if it_result is None or len(it_result) == 0:
                log.warning("Remote iterator result is empty, skipping")
                continue

            remote_result.append(it_result)

            remote_result[-1].address = range.address[i][1]
            remote_result[-1].group_id = i
            remote_result_len = len(remote_result[-1])
            log.debug("Remote obtained: {0} record(s)".format(remote_result_len))
            stats.counter.remote_records += remote_result_len
            ctx.monitor.add_counter("remote_records", remote_result_len)
            stats.counter.iterated_keys += remote_result_len
            ctx.monitor.add_counter("iterated_keys", remote_result_len)
            stats.counter.iterations += 1
            ctx.monitor.add_counter("iterations", 1)

        return local_result, remote_result

    except Exception as e:
        log.error("Iteration failed for: {0}@{1}: {2}".format(range.id_range, range.address, repr(e)))
        return None, None


def sort(ctx, local, remote, stats):
    """
    Runs sort routine for all iterator result
    """

    if remote is None or len(remote) == 0:
        log.debug("Sort skipped remote iterator results are empty")
        return local, remote

    try:
        assert all(local.id_range == r.id_range for r in remote), "Local range must equal remote range"

        log.info("Processing sorting local range: {0}".format(local.id_range))
        local.container.sort()
        stats.counter.sort += 1
        ctx.monitor.add_counter("sort", 1)

        for r in remote:
            log.info("Processing sorting remote range: {0}".format(r.id_range))
            r.container.sort()
            stats.counter.sort += 1
            ctx.monitor.add_counter("sort", 1)

        return local, remote
    except Exception as e:
        log.error("Sort of {0} failed: {1}".format(local.id_range, e))
        stats.counter.sort -= 1
        ctx.monitor.add_counter("sort", -1)
        return None, None


def diff(ctx, local, remote, stats):
    """
    Compute differences between local and remote results.
    TODO: We can compute up to CPU_NUM diffs at max in parallel
    """
    diffs = []
    total_diffs = 0
    for r in remote:
        try:
            if r is None or len(r) == 0:
                log.info("Remote container is empty, skipping")
                continue
            elif local is None or len(local) == 0:
                log.info("Local container is empty, recovering full range: {0}".format(local.id_range))
                result = r
            else:
                log.info("Computing differences for: {0}".format(local.id_range))
                result = local.diff(r)
                result.address = r.address
                result.group_id = r.group_id
            if len(result) > 0:
                diffs.append(result)
                result_len = len(result)
                stats.counter.diffs += result_len
                ctx.monitor.add_counter("diffs", result_len)
                total_diffs += result_len
            else:
                log.info("Resulting diff is empty, skipping")
        except Exception as e:
            log.error("Diff of {0} failed: {1}".format(local.id_range, e))
    log.info("Found {0} differences with remote nodes.".format(total_diffs))
    return diffs


def recover(ctx, splitted_results, stats):
    """
    Recovers difference between remote and local data.
    TODO: Group by diffs by host and process each group in parallel
    """
    result = True

    log.info("Recovering {0} keys".format(sum(len(d) for d in splitted_results)))

    local_node = elliptics_create_node(address=ctx.address, elog=ctx.elog)
    log.debug("Creating direct session: {0}".format(ctx.address))
    local_session = elliptics_create_session(node=local_node,
                                             group=ctx.group_id,
                                             )
    local_session.set_direct_id(*ctx.address)

    for diff in splitted_results:

        remote_node = elliptics_create_node(address=diff.address, elog=ctx.elog)
        log.debug("Creating direct session: {0}".format(diff.address))
        remote_session = elliptics_create_session(node=remote_node,
                                                  group=diff.eid.group_id,
                                                  )
        remote_session.set_direct_id(*diff.address)

        for batch_id, batch in groupby(enumerate(diff), key=lambda x: x[0] / ctx.batch_size):
            keys = [elliptics.Id(r.key, diff.eid.group_id) for _, r in batch]
            successes, failures = recover_keys(ctx, diff.address, diff.eid.group_id, keys, local_session, remote_session, stats)
            stats.counter.recovered_keys += successes
            ctx.monitor.add_counter("recovered_keys", successes)
            stats.counter.recovered_keys -= failures
            ctx.monitor.add_counter("recovered_keys", -failures)
            result &= (failures == 0)
            log.debug("Recovered batch: {0}/{1} of size: {2}/{3}".format(batch_id * ctx.batch_size + len(keys), len(diff), successes, failures))

    return result


def recover_keys(ctx, address, group_id, keys, local_session, remote_session, stats):
    """
    Bulk recovery of keys.
    """
    key_num = len(keys)
    size, read_count = (0, 0)
    async_write_results = []

    log.debug("Reading {0} keys".format(key_num))
    try:
        batch = remote_session.bulk_read_async(keys)
        for b in batch:
            b_data_len = len(b.data)
            async_write_results.append((local_session.write_data_async((b.id, b.timestamp, b.user_flags), b.data), b_data_len))
            size += b_data_len
            read_count += 1
    except Exception as e:
        log.debug("Bulk read failed: {0} keys: {1}".format(key_num, e))
        return 0, key_num

    log.debug("Writing {0} keys: {1} bytes".format(read_count, size))

    try:
        successes, failures, recovered_size, successes_size, failures_size = (0, 0, 0, 0, 0)
        for r, bsize in async_write_results:
            r.wait()
            recovered_size += bsize
            if r.successful():
                successes_size += bsize
                successes += 1
            else:
                failures_size += bsize
                failures += 1

        stats.counter.recovered_bytes += successes_size
        ctx.monitor.add_counter("recovered_bytes", successes_size)
        stats.counter.recovered_bytes -= failures_size
        ctx.monitor.add_counter("recovered_bytes", successes_size)
        return successes, failures
    except Exception as e:
        log.debug("Bulk write failed: {0} keys: {1}".format(key_num, e))
        stats.counter.recovered_bytes -= size
        ctx.monitor.add_counter("recovered_bytes", -size)
        return 0, key_num


def process_range((range, dry_run)):
    ctx = g_ctx

    stats_name = 'range_{0}'.format(range.id_range)
    stats = Stats(stats_name)

    ctx.monitor.add_timer(stats_name, "started")
    stats.timer.process('started')

    ctx.elog = elliptics.Logger(ctx.log_file, ctx.log_level)

    log.info("Running iterators")
    ctx.monitor.add_timer(stats_name, "iterator")
    stats.timer.process('iterator')
    it_local, it_remotes = run_iterators(ctx, range, stats)
    stats.timer.process('finished')
    ctx.monitor.add_timer(stats_name, "finished")

    if it_remotes is None or len(it_remotes) == 0:
        log.warning("Iterator results are empty, skipping")
        return True, stats

    ctx.monitor.add_timer(stats_name, "sort")
    stats.timer.process('sort')
    sorted_local, sorted_remotes = sort(ctx, it_local, it_remotes, stats)
    stats.timer.process('finished')
    ctx.monitor.add_timer(stats_name, "finished")
    assert len(sorted_remotes) >= len(it_remotes)

    log.info("Computing diff local vs remotes")
    ctx.monitor.add_timer(stats_name, "diff")
    stats.timer.process('diff')
    diff_results = diff(ctx, sorted_local, sorted_remotes, stats)
    stats.timer.process('finished')
    ctx.monitor.add_timer(stats_name, "finished")

    if diff_results is None or len(diff_results) == 0:
        log.warning("Diff results are empty, skipping")
        return True, stats

    log.info('Computing merge and splitting by node all remote results')
    ctx.monitor.add_timer(stats_name, "merge and split")
    stats.timer.process('merge and split')
    splitted_results = IteratorResult.merge(diff_results, ctx.tmp_dir)
    stats.timer.process('finished')
    ctx.monitor.add_timer(stats_name, "finished")

    result = True
    ctx.monitor.add_timer(stats_name, "recover")
    stats.timer.process('recover')
    if not dry_run:
        result = recover(ctx, splitted_results, stats)
    stats.timer.process('finished')
    ctx.monitor.add_timer(stats_name, "finished")

    return result, stats


def main(ctx):
    global g_ctx
    g_ctx = ctx
    stats_name = "main"
    result = True
    ctx.monitor.add_timer(stats_name, "started")
    g_ctx.stats.timer.main('started')

    log.debug("Groups: %s" % g_ctx.groups)

    g_ctx.group_id = g_ctx.routes.filter_by_address(g_ctx.address)[0].key.group_id

    log.info("Searching for ranges that %s store" % g_ctx.address)
    ranges = g_ctx.routes.get_ranges_by_address(g_ctx.address)
    log.debug("Recovery ranges: %d" % len(ranges))
    if not ranges:
        log.warning("No ranges to recover for address %s" % g_ctx.address)
        g_ctx.stats.timer.main('finished')
        return result

    processes = min(g_ctx.nprocess, len(ranges))
    pool = Pool(processes=processes, initializer=worker_init)
    log.debug("Created pool of processes: %d" % processes)

    try:
        for r, stats in pool.imap_unordered(process_range, ((r, g_ctx.dry_run) for r in ranges)):
            g_ctx.stats[stats.name] = stats
            result &= r

    except KeyboardInterrupt:
        log.error("Caught Ctrl+C. Terminating.")
        pool.terminate()
        pool.join()
        g_ctx.stats.timer.main('finished')
        ctx.monitor.add_timer(stats_name, "finished")
        return False
    else:
        log.info("Closing pool, joining threads.")
        pool.close()
        pool.join()

    g_ctx.stats.timer.main('finished')
    ctx.monitor.add_timer(stats_name, "finished")
    return result
