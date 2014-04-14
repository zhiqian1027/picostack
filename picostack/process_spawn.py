'''
Run on a local linux machine a subprocess in a daemon mode. Log the result.
'''
import re
import os
import sys
import time
import logging
from StringIO import StringIO
from datetime import datetime
from daemoncxt.daemon import DaemonContext
import errno
from daemoncxt.lockfile import LockTimeout
from daemoncxt.pidlockfile import TimeoutPIDLockFile
from subprocess import PIPE, Popen


logger = logging.getLogger(__name__)


class ExecProcessError(Exception):
    '''Raised if forking error or similar problem occurred.'''


NEW_LINE = '\n'
NUM_SECTION_LINES = 1000
HZ = os.sysconf(os.sysconf_names['SC_CLK_TCK'])
SPAWN_TRIES = 3


def strfdelta(tdelta, fmt):
    d = {"days": tdelta.days}
    d["hours"], rem = divmod(tdelta.seconds, 3600)
    d["minutes"], d["seconds"] = divmod(rem, 60)
    return fmt.format(**d)

LOCAL_TIME_FMT = '{days} days {hours}:{minutes}:{seconds}'


class ProcessUtil(object):

    __boot_time = None

    @classmethod
    def get_boot_time(cls):
        if cls.__boot_time is None:
            system_stats = open('/proc/stat').readlines()
            for line in system_stats:
                if line.startswith('btime'):
                    cls.__boot_time = int(line.split()[1])
        return cls.__boot_time

    @classmethod
    def get_birthtime_secs(cls, pid):
        process_stats = open('/proc/%d/stat' % int(pid)).read().split()
        age_from_boot_jiffies = int(process_stats[21])
        age_from_boot_timestamp = age_from_boot_jiffies / HZ
        age_timestamp = cls.get_boot_time() + age_from_boot_timestamp
        return age_timestamp

    @classmethod
    def list_processes(cls):
        '''See also man 5 proc'''
        pids = [pid for pid in os.listdir('/proc') if pid.isdigit() \
                and os.access(os.path.join('/proc', pid, 'status'), os.R_OK) \
                and os.access(os.path.join('/proc', pid, 'cmdline'), os.R_OK)]
        for pid in pids:
            proc_info = dict(id=pid)
            try:
                proc_info['cmdline'] = open(os.path.join('/proc', pid, 'cmdline'), 'rb').read()
                #proc_info['cwd'] = os.readlink(os.path.join('/proc', pid, 'cwd'))
                proc_info['running_since'] = ProcessUtil.get_birthtime_secs(pid)
                # Read process status
                proc_status = open(os.path.join('/proc', pid, 'status'), 'rb').read()
                for line in proc_status.split('\n'):
                    if not ':' in line:
                        continue
                    key, value = line.split(':', 1)
                    proc_info[key.strip()] = value.strip()
                yield proc_info
            except IOError:
                logger.debug('Can not read process (%d). Skipping..', pid)

    @classmethod
    def exec_process(cls, shell_command, report_filename, pidfile_path):

        def fork_parent(shell_command, report_filename, pidfile, error_message):
            """ Fork a child process.

                If the fork fails, raise a ``DaemonProcessDetachError``
                with ``error_message``.
                """
            try:
                pid = os.fork()
                if pid > 0:
                    logger.debug('Waiting for pidfile which means process '
                                 'was spawned.')
                    os.waitpid(pid, 0)
                    if os.path.exists(pidfile.path):
                        logger.debug('Giving it another try while waiting '
                                     'for pidfile..')
                        return pid
                    # Give it even more chances.. Waiting for th pidfile.
                    for try_num in xrange(SPAWN_TRIES):
                        print '.'
                        if os.path.exists(pidfile.path):
                            return pid
                        time.sleep(1)
                    raise ExecProcessError('Failed to spawn process '
                                           'after (%d) retries..' % SPAWN_TRIES)
                else:
                    with DaemonContext(
                        pidfile=pidfile,
                        stdout=sys.stdout,
                        stderr=sys.stderr,
                        detach_process=True) as process:
                        process_error = ''
                        # Write report file.
                        report = open(report_filename, 'a+')
                        started_at = datetime.now()
                        success = True
                        try:
                            cmd_args = shell_command.split(' ')
                            proc = Popen(cmd_args, stdout=PIPE, stderr=PIPE)
                            process_output, process_error = proc.communicate()
                            # TODO: check return code?
                        except Exception as exception:
                            stderr = StringIO()
                            process_error = repr(exception)
                            exc_type, exc_value, exc_traceback = sys.exc_info()
                            #traceback.print_exception(exc_type, exc_value, exc_traceback,
                            #      limit=3, file=stderr)
                            process_error += '\n' + stderr.getvalue().strip()
                            print process_error
                            success = False
                        time.sleep(5)
                        elapsed = datetime.now() - started_at
                        # TODO: gather /proc stats for current process
                        report.write('Elapsed time: %s \n' % \
                                     strfdelta(elapsed, LOCAL_TIME_FMT))
                        report.write('Your job looked like:\n')
                        report.write(shell_command + '\n')
                        if success:
                            report.write('Successfully completed.\n')
                        else:
                            report.write('Job failed with an error: %s.\n' % process_error)
                        report.write('The output (if any) follows:\n')
                        report.write('Process output was:\n')
                        report.write(process_output)
                        report.write('Process stderror was:\n')
                        report.write(process_error)
                        report.close()
                    # Exit child after work is done.
                    #os._exit(0)
                    exit(0)
            except OSError, exc:
                exc_errno = exc.errno
                exc_strerror = exc.strerror
                error = ExecProcessError(
                    "%(error_message)s: [%(exc_errno)d] %(exc_strerror)s" % vars())
                raise error
        pidfile = None
        try:
            if os.path.exists(pidfile_path):
                raise ExecProcessError('Error! A pidfile already exists: ' +
                                       pidfile_path)
            pidfile = cls.get_pidfile(pidfile_path)
            pid = fork_parent(shell_command, report_filename, pidfile,
                              error_message="Failed to fork")
            # TODO: think of using os.setsid()?
            return pid
        except LockTimeout:
            raise ExecProcessError('Process pidfile is locked: ' + pidfile_path)

    @classmethod
    def get_pidfile(cls, pidfile_path):
        '''Note that pidfile_path must be absolute'''
        return TimeoutPIDLockFile(
                pidfile_path,
                acquire_timeout=5,
                threaded=False)

    @classmethod
    def pid_exists(cls, pid):
        """Check whether pid exists in the current process table."""
        if pid < 0:
            return False
        try:
            os.kill(pid, 0)
        except OSError, e:
            return e.errno == errno.EPERM
        else:
            return True

    @classmethod
    def process_runs(cls, pidfile_path):
        logger.debug('Process runs? ' + pidfile_path)
        if os.path.exists(pidfile_path):
            try:
                pid = int(open(pidfile_path).read().strip())
                if cls.pid_exists(pid):
                    return True
            except ValueError:
                return False
        # TODO: try to acquire a lock based on pidfile?
        return False
