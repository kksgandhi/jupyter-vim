"""
Feature to get a buffer with jupyter output
"""

# Standard
from time import sleep

# Local
from jupyter_util import echom, prettify_execute_intput, str_to_vim, \
                         strip_color_escapes, unquote_string
try:
    from queue import Empty
except ImportError:
    from Queue import Empty

# Process local
import vim


class Monitor():
    """Jupyter kernel monitor buffer and message line"""
    def __init__(self, session_info):
        self.session_info = session_info
        self.cmd = None
        self.cmd_id = None
        self.cmd_count = 0

    def monitorable(self, fct):
        """Decorator to monitor messages"""
        def wrapper(*args, **kwargs):
            # Check in
            if not self.session_info.kernel_client.check_connection_or_warn():
                return

            # Call
            fct(*args, **kwargs)

            # Clause
            self.session_info.vim_client.set_monitor_bools()
            if (not self.session_info.vim_client.verbose
                    and not self.session_info.vim_client.monitor_console):
                return

            # Launch update threads
            self.update_msgs()
        return wrapper

    def update_msgs(self):
        """Launch pending messages grabbers (Sync but not for long)
        Param: console (boolean): should I update console
            prompt  (boolean): should I update prompt
            last_cmd (string): not used already
        """
        # Open the Jupyter terminal in vim, and move cursor to it
        if -1 == vim.eval('jupyter#monitor_console#OpenJupyterTerm()'):
            echom('__jupyter_term__ failed to open!', 'Error')
            return

        # Define time: thread (additive) sleep and timer wait
        timer_intervals = self.session_info.vim_client.get_timer_intervals()
        thread_intervals = [50]
        for i in range(len(timer_intervals)-1):
            thread_intervals.append(timer_intervals[i+1] - timer_intervals[i] - 50)

        # Create thread
        self.session_info.sync.start_thread(
            target=self.thread_fetch_msgs,
            args=[thread_intervals])

        # Launch timers
        for sleep_ms in timer_intervals:
            vim_cmd = ('call timer_start(' + str(sleep_ms) +
                       ', "jupyter#monitor_console#UpdateConsoleBuffer")')
            vim.command(vim_cmd)

    def thread_fetch_msgs(self, intervals):
        """Update message that timer will append to console message
        """
        io_cache = list()
        for sleep_ms in intervals:
            # Sleep ms
            if self.session_info.sync.check_stop():
                return
            sleep(sleep_ms / 1000)
            if self.session_info.sync.check_stop():
                return

            # Get messages
            msgs = self.session_info.kernel_client.get_pending_msgs()
            io_new = parse_messages(self.session_info, msgs)

            # Insert code line Check not already here (check with substr 'Py [')
            if (self.cmd is not None
                    and len(io_new) != 0
                    and not any(self.session_info.lang.prompt_in[:4] in msg
                                for msg in io_new + io_cache)):
                # Get cmd number from id
                try:
                    reply = self.session_info.kernel_client.get_reply_msg(self.cmd_id)
                    line_number = reply['content'].get('execution_count', 0)
                except (Empty, KeyError, TypeError):
                    line_number = -1
                s_new_input = prettify_execute_intput(
                        line_number, self.cmd, self.session_info.lang.prompt_in)
                io_new.insert(0, s_new_input)

            # Append just new
            _ = [self.session_info.sync.line_queue.put(s_in)
                    for s_in in io_new
                    if s_in not in io_cache]
            # Update cache
            io_cache = list(set().union(io_cache, io_new))

    def timer_write_console_msgs(self):
        """Write kernel <-> vim messages to console buffer"""
        # Check in
        if self.session_info.sync.line_queue.empty():
            return
        if (not self.session_info.vim_client.monitor_console
                and not self.session_info.vim_client.verbose):
            return

        # Get buffer (same indexes as vim)
        if self.session_info.vim_client.monitor_console:
            b_nb = int(vim.eval('bufnr("__jupyter_term__")'))
            buf = vim.buffers[b_nb]

        # Append mesage to jupyter terminal buffer
        while not self.session_info.sync.line_queue.empty():
            msg = self.session_info.sync.line_queue.get_nowait()
            for line in msg.splitlines():
                line = unquote_string(str_to_vim(line))
                if self.session_info.vim_client.monitor_console:
                    buf.append(line)
                if self.session_info.vim_client.verbose:
                    echom(line)

        # Update view (moving cursor)
        if self.session_info.vim_client.monitor_console:
            cur_win = vim.eval('win_getid()')
            term_win = vim.eval('bufwinid({})'.format(str(b_nb)))
            vim.command('call win_gotoid({})'.format(term_win))
            vim.command('normal! G')
            vim.command('call win_gotoid({})'.format(cur_win))


def monitor_decorator(fct):
    """Redirect to self.monitor decorator"""
    def wrapper(self, *args, **kwargs):
        self.monitor.monitorable(fct)(self, *args, **kwargs)
    return wrapper


def parse_messages(session_info, msgs):
    """Message handler for Jupyter protocol (Async)

    Takes all messages on the I/O Public channel, including stdout, stderr,
    etc.
    Returns: a list of the formatted strings of their content.

    See also: <http://jupyter-client.readthedocs.io/en/stable/messaging.html>
    """
    # pylint: disable=too-many-branches
    # TODO session_info is not perfectly async
    # TODO remove complexity
    res = list()
    for msg in msgs:
        s_line = ''
        default_count = session_info.monitor.cmd_count
        if 'msg_type' not in msg['header']:
            continue
        msg_type = msg['header']['msg_type']

        if msg_type == 'status':
            # I don't care status (idle or busy)
            continue

        if msg_type == 'stream':
            # Get data
            text = strip_color_escapes(msg['content']['text'])
            line_number = msg['content'].get('execution_count', default_count)
            # Set prompt
            if msg['content'].get('name', 'stdout') == 'stderr':
                prompt = 'Err[{:d}]: '.format(line_number)
                dots = (' ' * (len(prompt.rstrip()) - 4)) + '...x '
            else:
                prompt = 'Out[{:d}]: '.format(line_number)
                dots = (' ' * (len(prompt.rstrip()) - 4)) + '...< '
            s_line = prompt
            # Add continuation line, if necessary
            s_line += text.rstrip().replace('\n', '\n' + dots)
            # Set cmd_count: if it changed
            if line_number != default_count:
                session_info.monitor.cmd_count = line_number

        elif msg_type == 'display_data':
            s_line += msg['content']['data']['text/plain']

        elif msg_type in ('execute_input', 'pyin'):
            line_number = msg['content'].get('execution_count', default_count)
            cmd = msg['content']['code']
            s_line = prettify_execute_intput(line_number, cmd, session_info.lang.prompt_in)
            # Set cmd_count: if it changed
            if line_number != default_count:
                session_info.monitor.cmd_count = line_number

        elif msg_type in ('execute_result', 'pyout'):
            # Get the output
            line_number = msg['content'].get('execution_count', default_count)
            s_line = session_info.lang.prompt_out.format(line_number)
            s_line += msg['content']['data']['text/plain']
            # Set cmd_count: if it changed
            if line_number != default_count:
                session_info.monitor.cmd_count = line_number

        elif msg_type in ('error', 'pyerr'):
            s_line = "\n".join((strip_color_escapes(x) for x in msg['content']['traceback']))

        elif msg_type == 'input_request':
            session_info.vim_client.thread_echom(
                'python input not supported in vim.', style='Error')
            continue  # unsure what to do here... maybe just return False?

        else:
            session_info.vim_client.thread_echom("Message type {} unrecognized!".format(msg_type))
            continue

        # List all messages
        res.append(s_line)

    return res
