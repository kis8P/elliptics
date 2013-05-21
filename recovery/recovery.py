#!/usr/bin/env python

__doc__ = \
    """
    XXX:
    """

import os
import sys
import logging as log

from recover.route import RouteList, Address
from recover.stat import Stats
from recover.time import Time
from recover.utils.misc import elliptics_create_node, elliptics_create_session

# XXX: change me before BETA
sys.path.insert(0, "bindings/python/")
import elliptics

log.getLogger()

TYPE_MERGE = 'merge'
TYPE_DC = 'dc'
ALLOWED_TYPES = (TYPE_MERGE, TYPE_DC)

STAT_NONE = 'none'
STAT_TEXT = 'text'
ALLOWED_STATS = (STAT_NONE, STAT_TEXT)

if __name__ == '__main__':
    from recover.ctx import Ctx
    from optparse import OptionParser

    parser = OptionParser()
    parser.usage = "%prog [options] TYPE"
    parser.description = __doc__
    parser.add_option("-l", "--log", dest="elliptics_log", default='/dev/stderr', metavar="FILE",
                      help="Output log messages from library to file [default: %default]")
    parser.add_option("-L", "--log-level", action="store", dest="elliptics_log_level", default="0",
                      help="Elliptics client verbosity [default: %default]")
    parser.add_option("-r", "--remote", action="store", dest="elliptics_remote", default="127.0.0.1:1025:2",
                      help="Elliptics node address [default: %default]")
    parser.add_option("-g", "--groups", action="store", dest="elliptics_groups", default=None,
                      help="Comma separated list of groups [default: %default]")
    parser.add_option("-t", "--timestamp", action="store", dest="timestamp", default="0",
                      help="Recover keys created/modified since [default: %default]")
    parser.add_option("-b", "--batch-size", action="store", dest="batch_size", default="1024",
                      help="Number of keys in read_bulk/write_bulk batch [default: %default]")
    parser.add_option("-d", "--debug", action="store_true", dest="debug", default=False,
                      help="Enable debug output [default: %default]")
    parser.add_option("-s", "--stat", action="store", dest="stat", default="text",
                      help="Statistics output format: {0} [default: %default]".format("/".join(ALLOWED_STATS)))
    parser.add_option("-D", "--dir", dest="tmp_dir", default='/var/tmp/', metavar="DIR",
                      help="Temporary directory for iterators' results [default: %default]")
    parser.add_option("-n", "--nprocess", action="store", dest="nprocess", default="1",
                      help="Number of subprocesses [default %default]")

    (options, args) = parser.parse_args()

    if options.debug:
        log.getLogger().setLevel(log.DEBUG)

    if len(args) > 1:
        raise ValueError("Too many arguments passed: {0}, expected: 1".format(len(args)))
    elif len(args) == 0:
        raise ValueError("Please specify one of following types: {0}".format(ALLOWED_TYPES))

    if args[0].lower() not in ALLOWED_TYPES:
        raise ValueError("Unknown type: '{0}', allowed: {1}".format(args[0], ALLOWED_TYPES))
    recovery_type = args[0].lower()

    log.info("Initializing context")
    ctx = Ctx()

    log.info("Initializing stats")
    ctx.stats = Stats(name='global')

    try:
        ctx.address = Address.from_host_port_family(options.elliptics_remote)
    except Exception as e:
        raise ValueError("Can't parse host:port:family: '{0}': {1}".format(
            options.elliptics_remote, repr(e)))
    log.info("Using host:port:family: {0}".format(ctx.address))

    try:
        if options.elliptics_groups != None:
            ctx.groups = map(int, options.elliptics_groups.split(','))
        else:
            ctx.groups = []
    except Exception as e:
        raise ValueError("Can't parse grouplist: '{0}': {1}".format(
            options.elliptics_groups, repr(e)))
    log.info("Using group list: {0}".format(ctx.groups))

    try:
        ctx.timestamp = Time.from_epoch(options.timestamp)
    except Exception as e:
        raise ValueError("Can't parse timestamp: '{0}': {1}".format(
            options.timestamp, repr(e)))
    log.info("Using timestamp: {0}".format(ctx.timestamp))

    try:
        ctx.batch_size = int(options.batch_size)
    except Exception as e:
        raise ValueError("Can't parse batchsize: '{0}': {1}".format(
            options.batch_size, repr(e)))
    log.info("Using batch_size: {0}".format(ctx.batch_size))

    try:
        ctx.log_file = options.elliptics_log
        ctx.log_level = int(options.elliptics_log_level)
    except Exception as e:
        raise ValueError("Can't parse log_level: '{0}': {1}".format(
            options.elliptics_log_level, repr(e)))
    log.info("Using elliptics client log level: {0}".format(ctx.log_level))

    ctx.tmp_dir = options.tmp_dir
    if not os.access(ctx.tmp_dir, os.W_OK):
        raise ValueError("Don't have write access to: {0}".format(options.tmp_dir))
    log.info("Using tmp directory: {0}".format(ctx.tmp_dir))

    try:
        ctx.nprocess = int(options.nprocess)
    except Exception as e:
        raise ValueError("Can't parse nprocess: '{0}': {1}".format(
            options.nprocess, repr(e)))

    if options.stat not in ALLOWED_STATS:
        raise ValueError("Unknown output format: '{0}'. Available formats are: {1}".format(
            options.stat, ALLOWED_STATS))

    log.debug("Using following context:\n{0}".format(ctx))

    log.info("Setting up elliptics client")

    log.debug("Creating logger")
    ctx.elog = elliptics.Logger(ctx.log_file, int(ctx.log_level))

    log.debug("Creating node")
    node = elliptics_create_node(address=ctx.address, elog=ctx.elog)

    log.debug("Creating session for: {0}".format(ctx.address))
    session = elliptics_create_session(node=node, group=0)

    log.warning("Parsing routing table".format(ctx.address))
    ctx.routes = RouteList.from_session(session)
    log.debug(ctx.routes)
    log.debug("Total routes: {0}".format(len(ctx.routes)))

    if recovery_type == TYPE_MERGE:
        from recovery_merge import main
        result = main(ctx)
    elif recovery_type == TYPE_DC:
        from dc_recovery import main
        result = main(ctx)
    else:
        raise RuntimeError("Type '{0}' is not supported for now".format(recovery_type))

    if options.stat == STAT_TEXT:
        print ctx.stats

    exit(not result)