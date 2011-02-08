from __future__ import division, print_function

import os, sys

if sys.version_info[0] < 3 and sys.version_info[1] < 7:
    sys.stderr.write('wemd requires at least Python version 2.7\n')
    sys.exit(1)

import logging
log = logging.getLogger('wemd_cli')
import argparse

# We must prefer to load the wemd package over this script;
# this is only a problem for a program called "wemd.py"
try:
    wemd_lib_path = os.environ['WEMDLIB']
except KeyError:
    wemd_lib_path = os.path.join(os.path.dirname(os.path.realpath(__file__)), '..', 'src')
log.debug('prepending %r to sys.path' % wemd_lib_path)
sys.path.insert(0, wemd_lib_path)

import wemd
from wemd.util.config_dict import ConfigDict
from wemd.util import extloader

import numpy, operator, itertools

# Runtime config file management
ENV_RUNTIME_CONFIG  = 'WEMDRC'
RC_DEFAULT_FILENAME = 'wemd.cfg'

def read_config(filename = None):
    if filename is None:
        filename = RC_DEFAULT_FILENAME
    
    cdict = ConfigDict()
    cdict.read_config_file(filename)
    
    return cdict

def load_sim_manager(runtime_config):
    drivername = runtime_config.get('drivers.sim_manager', 'default')
    if drivername.lower() == 'default':
        from wemd.sim_manager import WESimManager
        return WESimManager(runtime_config)
    else:
        pathinfo = runtime_config.get_pathlist('drivers.module_path')
        return extloader.get_object(drivername,pathinfo)(runtime_config)

def cmd_init(sim_manager, args, aux_args):
    if aux_args:
        log.error('unexpected command line argument(s) ignored: %r' % aux_args)
        sys.exit(os.EX_USAGE)
        
    # Create HDF5 data file
    sim_manager.load_data_manager()
    sim_manager.load_we_driver()
    sim_manager.runtime_config.require('data.h5file')
    h5file = sim_manager.runtime_config.get_path('data.h5file')
    if os.path.exists(h5file):
        if args.force:
            sys.stdout.write('Deleting existing HDF5 file %r.\n' % h5file)
            os.unlink(h5file)
        else:
            sys.stderr.write('HDF5 file %r already exists; exiting.\n' % h5file)
            sys.exit(os.EX_USAGE)
    
    sys.stdout.write('Creating HDF5 file %r.\n' % h5file)
    sim_manager.data_manager.prepare_backing()
    
    # Load system driver and report a few initial statistics
    sim_manager.load_system_driver()
    system = sim_manager.system
    region_set = system.region_set
    
    sys.stdout.write('\nDistribution of initial states:\n')
    sys.stdout.write('{:<16} {:<12} {:<52}\n'.format('Name', 'Probability', 'Coordinates'))
    
    tprob = 0.0
    for (name, prob, pcoord, bin) in system.initial_states:
        sys.stdout.write('{:<16} {:<12g} {!s:<52}\n'.format(name, prob, list(pcoord)))
        tprob += prob

    MACHEPS = numpy.finfo(numpy.float64).eps
    if abs(1.0 - tprob) > MACHEPS*len(system.initial_states):
        sys.stderr.write('Initial probabilities do not sum to one.')
        sys.exit(1)

    # Create initial segments
    # First assign to bins
    for (istate, (name, prob, pcoord, bin)) in enumerate(system.initial_states):
        target_count = bin.target_count
        for i in xrange(0, target_count):
            bin.add(wemd.Particle(pcoord=pcoord, weight=prob/target_count, source_id=istate))
        sys.stdout.write('%d replicas from initial point %r\n' % (target_count,name))

    iprobtot = region_set.weight
    all_bins = region_set.get_all_bins()
    bin_occupancies = numpy.array(map(operator.attrgetter('count'), all_bins))
    target_occupancies = numpy.array(map(operator.attrgetter('target_count'), all_bins))
    
    sys.stdout.write('''
Total bins:             {:d}
Initial particles:      {:d} in {:d} bins, total weight = {:g}
Total target particles: {:d}
'''.format(len(all_bins),
           sum(bin_occupancies), len(bin_occupancies[bin_occupancies > 0]), iprobtot, 
           sum(target_occupancies)))
    
    # The user-side check for this was above; this is an assertion that the above assignment to bins 
    # and division of probability is correct
    assert abs(sim_manager.system.region_set.weight - tprob) < MACHEPS*sum(bin_occupancies)
    
    # Now that we're in bins, let's create some segments and commit them to the data manager
    segments = []
    for (seg_id, particle) in enumerate(itertools.chain(*all_bins)):
        segment = wemd.Segment(weight = particle.weight,
                               source_id = particle.source_id,
                               pcoord = [particle.pcoord],
                               p_parent_id = -(particle.source_id+1),
                               parent_ids = set((-(particle.source_id+1),)),
                               status = wemd.Segment.SEG_STATUS_PREPARED)
        segments.append(segment)
            
    sim_manager.data_manager.prepare_iteration(1, segments, system.pcoord_ndim, system.pcoord_len,
                                               system.pcoord_dtype)
    sim_manager.data_manager.flush_backing()

    sim_manager.system.region_set.clear()    
    sys.stdout.write('Simulation prepared.\n')
        
def cmd_run(sim_manager, args, aux_args):
    # Let the work manager parse any remaining command-line arguments
    sim_manager.load_work_manager()
    aux_args = sim_manager.work_manager.parse_aux_args(aux_args)
    
    sim_manager.load_data_manager()
    sim_manager.data_manager.open_backing()
        
    sim_manager.load_system_driver()
    sim_manager.load_we_driver()
    sim_manager.load_propagator()
    
    if aux_args:
        log.warning('unexpected command line argument(s) ignored: %r' % aux_args)
    
    try:
        rc = sim_manager.run()
    except:
        sim_manager.work_manager.shutdown(-1)
        raise
    else:
        sim_manager.work_manager.shutdown(0)
        sys.exit(rc)   

    

parser = argparse.ArgumentParser()
parser.add_argument('-r', '--rcfile', metavar='RCFILE', dest='run_config_file',
                    help='use RCFILE as the WEMD run-time configuration file (default: %s)' 
                          % RC_DEFAULT_FILENAME)
parser.add_argument('--verbose', dest='verbose_mode', action='store_true',
                    help='emit extra information')
parser.add_argument('--debug', dest='debug_mode', action='store_true',
                    help='enable extra checks and emit copious information')
parser.add_argument('--profile', dest='profile_mode', action='store_true',
                    help='run this process under the Python profiler')
parser.add_argument('--version', action='version', version='WEMD version %s' % wemd.version)

subparsers = parser.add_subparsers()

parser_init =    subparsers.add_parser('init', help='initialize a new simulation')
parser_init.add_argument('--force', dest='force', action='store_true',
                         help='overwrite any existing simulation data')
parser_init.set_defaults(func=cmd_init)

parser_run =     subparsers.add_parser('run', help='start/continue a simulation')
parser_run.add_argument('--oneseg', dest='only_one_segment', action='store_true',
                        help='only propagate one segment (useful for debugging problems in propagators)')
parser_run.add_argument('--work-manager', dest='work_manager_name', 
                        help='use the given work manager to propagate segments (e.g. serial, threads, tcpip,'
                            +' or name a Python class; default: threads)')
parser_run.set_defaults(func=cmd_run)

parser_status =  subparsers.add_parser('status', help='report simulation status')


# Parse command line arguments
(args, aux_args) = parser.parse_known_args()

# Handle forward configuration of logging
import logging.config
logging_config = {'version': 1, 'incremental': False,
                  'formatters': {'standard': {'format': '  -- %(levelname)-8s -- %(message)s'},
                                 'debug':    {'format': '''\
  -- %(levelname)-8s %(asctime)24s PID %(process)-12d TID %(thread)-20d 
     %(pathname)s:%(lineno)d [%(funcName)s()] 
       %(message)s'''}},
                  'handlers': {'console': {'class': 'logging.StreamHandler',
                                           'stream': 'ext://sys.stdout',
                                           'formatter': 'standard'}},
                  'loggers': {'wemd': {'handlers': ['console'], 'propagate': False},
                              'wemd_cli': {'handlers': ['console'], 'propagate': False}},
                  'root': {'handlers': ['console']}}

if args.verbose_mode:
    logging_config['root']['level'] = 'INFO'
if args.debug_mode:
    logging_config['root']['level'] = 'DEBUG'
    logging_config['handlers']['console']['formatter'] = 'debug'

logging.config.dictConfig(logging_config)
logging_config['incremental'] = True


# Read runtime configuration file
runtime_config = read_config(args.run_config_file)

# Merge command line arguments into runtime config (for convenience)
runtime_config.update({'args.%s' % key : value for (key,value) in args.__dict__.viewitems() if not key.startswith('_')})

# Load SimManager
sim_manager = load_sim_manager(runtime_config)

# Branch to appropriate function
if args.profile_mode:
    import cProfile, pstats
    try:
        cProfile.run('args.func(sim_manager,args,aux_args)', 'profile.dat')
    finally:
        stats = pstats.Stats('profile.dat')
        #stats.sort_stats('cumulative')
        stats.sort_stats('time')
        stats.print_stats()
else:
    try:
        args.func(sim_manager, args, aux_args)
    except KeyboardInterrupt:
        sys.stderr.write('Interrupted.\n')
        sys.exit(1)
    except Exception as e:
        # The following won't show up if the log isn't set up properly
        log.error(str(e))
        sys.stderr.write('ERROR: {!s}\n'.format(e))
        if args.debug_mode or args.verbose_mode:
            import traceback
            traceback.print_exc()
        sys.exit(1)
    else:
        sys.exit(0)