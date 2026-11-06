# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Martin J. Gallagher
"""What vim, less and top write to an xterm-256color terminal, recorded.

Each was run on a pty of the size given, with ``TERM=xterm-256color``,
driven by a few keystrokes, and everything it wrote was kept: ``data`` is
the whole session, up to and including the program exiting, and
``running`` is how much of it had arrived while the program was still on
screen.  The expected screens are what tmux -- an independent emulator --
shows for the same bytes, so they are not this project checking itself.

* vim: a file with ``syntax on`` and ``set number``; three lines down,
  ``o`` to open a line, some text, Esc -- then ``:q!``.  Colours, scroll
  region, insert line, the alternate screen, and queries it sends (DA,
  cursor position, a DCS and two OSC colour queries).
* less: ``less -R`` on a file with SGR colours (16, 256 and 24-bit) and
  backspace overstrike; a page down, a search, then ``q``.
* top: one refresh, then ``q``.  Lines that exactly fill the width followed
  by an erase to the end of the line -- the pending-wrap case.
"""

RECORDINGS = {
    'vim': {
        "rows": 12,
        "cols": 40,
        "running": 1297,
        "data": (
            b'\x1b[?1049h\x1b[22;0;0t\x1b[>4;2m\x1b[?1h\x1b=\x1b[?2004h\x1b[?1004h'
            b'\x1b[1;12r\x1b[?12h\x1b[?12l\x1b[22;2t\x1b[22;1t\x1b[27m\x1b[23m\x1b['
            b'29m\x1b[m\x1b[H\x1b[2J\x1b[?25l\x1b[12;1H"notes.py" 34L, 1469B\x1b[2;'
            b'1H\xe2\x96\xbd\x1b[6n\x1b[2;1H  \x1b[3;1H\x1bPzz\x1b\\\x1b[0%m\x1b[6n'
            b'\x1b[3;1H           \x1b[1;1H\x1b[>c\x1b]10;?\x07\x1b]11;?\x07\x1b[1;'
            b'1H\x1b[38;5;130m  1 \x1b[m\x1b[34m# Notes for the terminal test\x1b[m'
            b'\r\n\x1b[38;5;130m  2 \x1b[m\x1b[2;5H\x1b[K\x1b[3;1H\x1b[38;5;130m  3'
            b' def\x1b[m \x1b[36mgreet\x1b[m(name):\x1b[3;21H\x1b[K\x1b[4;1H\x1b[38'
            b';5;130m  4 \x1b[m    \x1b[38;5;130mreturn\x1b[m \x1b[31m"hello, "\x1b'
            b'[m + name\r\n\x1b[38;5;130m  5 \r\n  6 \x1b[mline 01 of a file that '
            b'\x1b[38;5;130mis\x1b[m long enougg\x1b[7;1H\x1b[38;5;130m    \x1b[mh '
            b'to scroll\r\n\x1b[38;5;130m  7 \x1b[mline 02 of a file that \x1b[38;5'
            b';130mis\x1b[m long enougg\x1b[9;1H\x1b[38;5;130m    \x1b[mh to scroll'
            b'\r\n\x1b[38;5;130m  8 \x1b[mline 03 of a file that \x1b[38;5;130mis'
            b'\x1b[m long enougg\x1b[11;1H\x1b[38;5;130m    \x1b[mh to scroll\x1b[1'
            b';5H\x1b[?25h\x1b[?4m\x1b[2;5H\x1b[3;5H\x1b[4;5H\x1b[?25l\x1b[12;1H'
            b'\x1b[1m-- INSERT --\x1b[m\x1b[12;13H\x1b[K\x1b[5;5Hnew line here\x1b['
            b'6;5H\x1b[K\x1b[7;3H\x1b[38;5;130m7\x1b[m\x1b[1Cline 01 of a file that'
            b' \x1b[38;5;130mis\x1b[m long enougg\x1b[8;1H\x1b[38;5;130m   \x1b[m'
            b'\x1b[1Ch to scroll\x1b[8;16H\x1b[K\x1b[9;3H\x1b[38;5;130m8\x1b[m\x1b['
            b'1Cline 02 of a file that \x1b[38;5;130mis\x1b[m long enougg\x1b[10;1H'
            b'\x1b[38;5;130m   \x1b[m\x1b[1Ch to scroll\x1b[10;16H\x1b[K\x1b[11;3H'
            b'\x1b[38;5;130m9\x1b[m\x1b[1Cline 03 of a file that \x1b[38;5;130mis'
            b'\x1b[m long enoug\x1b[11;3H\x1b[38;5;130m  \x1b[m\x1b[94m@           '
            b'                        \x1b[5;18H\x1b[?25h\x1b[m\x1b[12;1H\x1b[K\x1b'
            b'[5;17H\x1b[?25l\x1b[?25h\x1b[?25l\x1b[12;1H:q!\r\x1b[?2004l\x1b[>4;m'
            b'\x1b[23;2t\x1b[23;1t\x1b[12;1H\x1b[K\x1b[12;1H\x1b[?1004l\x1b[?2004l'
            b'\x1b[?1l\x1b>\x1b[?1049l\x1b[23;0;0t\x1b[?25h\x1b[>4;m'
        ),
        "running_screen": [
            '  1 # Notes for the terminal test',
            '  2',
            '  3 def greet(name):',
            '  4     return "hello, " + name',
            '  5 new line here',
            '  6',
            '  7 line 01 of a file that is long enoug',
            '    h to scroll',
            '  8 line 02 of a file that is long enoug',
            '    h to scroll',
            '    @',
            '',
        ],
        "running_cursor": (16, 4),
        "exit_screen": [
            '',
            '',
            '',
            '',
            '',
            '',
            '',
            '',
            '',
            '',
            '',
            '',
        ],
        "exit_cursor": (0, 0),
    },
    'less': {
        "rows": 10,
        "cols": 40,
        "running": 1646,
        "data": (
            b'\x1b[?1049h\x1b[22;0;0t\x1b[?1h\x1b=\r\x1b[1;31mred bold\x1b[0m plain'
            b' \x1b[38;5;33mblue256\x1b[0m \x1b[38;2;10;200;10mtruecolour\x1b[0m'
            b'\x1b[m\r\n\x1b[1mBold\x1b[0m and \x1b[4munder\x1b[24m\x1b[m\r\nline 0'
            b'1 of a paged file\x1b[m\r\nline 02 of a paged file\x1b[m\r\nline 03 o'
            b'f a paged file\x1b[m\r\nline 04 of a paged file\x1b[m\r\nline 05 of a'
            b' paged file\x1b[m\r\nline 06 of a paged file\x1b[m\r\nline 07 of a pa'
            b'ged file\x1b[m\r\n\x1b[7mpaged.txt\x1b[27m\x1b[K\r\x1b[Kline 08 of a '
            b'paged file\x1b[m\r\nline 09 of a paged file\x1b[m\r\nline 10 of a pag'
            b'ed file\x1b[m\r\nline 11 of a paged file\x1b[m\r\nline 12 of a paged '
            b'file\x1b[m\r\nline 13 of a paged file\x1b[m\r\nline 14 of a paged fil'
            b'e\x1b[m\r\nline 15 of a paged file\x1b[m\r\nline 16 of a paged file'
            b'\x1b[m\r\n:\x1b[K\r\x1b[K/\x1b[Kl\x08l\x1b[Ki\x08i\x1b[Kn\x08n\x1b[Ke'
            b'\x08e\x1b[K \x08 \x1b[K2\x082\r\x1b[K\x1b[1;1Hline 08 of a paged file'
            b'\x1b[m\r\n\x1b[2;1Hline 09 of a paged file\x1b[m\r\n\x1b[3;1Hline 10 '
            b'of a paged file\x1b[m\r\n\x1b[4;1Hline 11 of a paged file\x1b[m\r\n'
            b'\x1b[5;1Hline 12 of a paged file\x1b[m\r\n\x1b[6;1Hline 13 of a paged'
            b' file\x1b[m\r\n\x1b[7;1Hline 14 of a paged file\x1b[m\r\n\x1b[8;1Hlin'
            b'e 15 of a paged file\x1b[m\r\n\x1b[9;1Hline 16 of a paged file\x1b[m'
            b'\r\n\x1b[10;1H\x1b[1;1Hline 08 of a paged file\x1b[m\r\n\x1b[2;1Hline'
            b' 09 of a paged file\x1b[m\r\n\x1b[3;1Hline 10 of a paged file\x1b[m\r'
            b'\n\x1b[4;1Hline 11 of a paged file\x1b[m\r\n\x1b[5;1Hline 12 of a pag'
            b'ed file\x1b[m\r\n\x1b[6;1Hline 13 of a paged file\x1b[m\r\n\x1b[7;1Hl'
            b'ine 14 of a paged file\x1b[m\r\n\x1b[8;1Hline 15 of a paged file\x1b['
            b'm\r\n\x1b[9;1Hline 16 of a paged file\x1b[m\r\n\x1b[10;1H...skipping.'
            b'..\r\n\x1b[7mline 2\x1b[27m0 of a paged file\x1b[m\r\n\x1b[7mline 2'
            b'\x1b[27m1 of a paged file\x1b[m\r\n\x1b[7mline 2\x1b[27m2 of a paged '
            b'file\x1b[m\r\n\x1b[7mline 2\x1b[27m3 of a paged file\x1b[m\r\n\x1b[7m'
            b'line 2\x1b[27m4 of a paged file\x1b[m\r\n\x1b[7mline 2\x1b[27m5 of a '
            b'paged file\x1b[m\r\n\x1b[7mline 2\x1b[27m6 of a paged file\x1b[m\r\n'
            b'\x1b[7mline 2\x1b[27m7 of a paged file\x1b[m\r\n\x1b[7mline 2\x1b[27m'
            b'8 of a paged file\x1b[m\r\n:\x1b[K\r\x1b[K\x1b[?1l\x1b>\x1b[?1049l'
            b'\x1b[23;0;0t'
        ),
        "running_screen": [
            'line 20 of a paged file',
            'line 21 of a paged file',
            'line 22 of a paged file',
            'line 23 of a paged file',
            'line 24 of a paged file',
            'line 25 of a paged file',
            'line 26 of a paged file',
            'line 27 of a paged file',
            'line 28 of a paged file',
            ':',
        ],
        "running_cursor": (1, 9),
        "exit_screen": [
            '',
            '',
            '',
            '',
            '',
            '',
            '',
            '',
            '',
            '',
        ],
        "exit_cursor": (0, 0),
    },
    'top': {
        "rows": 12,
        "cols": 60,
        "running": 1970,
        "data": (
            b'\x1b[?1h\x1b=\x1b[?25l\x1b[H\x1b[2J\x1b(B\x1b[mtop - 17:55:18 up 29 m'
            b'in,  0 user,  load average: 0.05, 0.07\x1b(B\x1b[m\x1b[39;49m\x1b(B'
            b'\x1b[m\x1b[39;49m\x1b[K\r\nTasks:\x1b(B\x1b[m\x1b[39;49m\x1b[1m  78 '
            b'\x1b(B\x1b[m\x1b[39;49mtotal,\x1b(B\x1b[m\x1b[39;49m\x1b[1m   1 \x1b('
            b'B\x1b[m\x1b[39;49mrunning,\x1b(B\x1b[m\x1b[39;49m\x1b[1m  77 \x1b(B'
            b'\x1b[m\x1b[39;49msleeping,\x1b(B\x1b[m\x1b[39;49m\x1b[1m   0 \x1b(B'
            b'\x1b[m\x1b[39;49mstopped,\x1b(B\x1b[m\x1b[39;49m\x1b[1m   \x1b(B\x1b['
            b'm\x1b[39;49m\x1b(B\x1b[m\x1b[39;49m\x1b[K\r\n%Cpu(s):\x1b(B\x1b[m\x1b'
            b'[39;49m\x1b[1m  0.0 \x1b(B\x1b[m\x1b[39;49mus,\x1b(B\x1b[m\x1b[39;49m'
            b'\x1b[1m  2.4 \x1b(B\x1b[m\x1b[39;49msy,\x1b(B\x1b[m\x1b[39;49m\x1b[1m'
            b'  0.0 \x1b(B\x1b[m\x1b[39;49mni,\x1b(B\x1b[m\x1b[39;49m\x1b[1m 97.6 '
            b'\x1b(B\x1b[m\x1b[39;49mid,\x1b(B\x1b[m\x1b[39;49m\x1b[1m  0.0 \x1b(B'
            b'\x1b[m\x1b[39;49mwa,\x1b(B\x1b[m\x1b[39;49m\x1b[1m  0.0 \x1b(B\x1b[m'
            b'\x1b[39;49mh\x1b(B\x1b[m\x1b[39;49m\x1b(B\x1b[m\x1b[39;49m\x1b[K\r\nM'
            b'iB Mem :\x1b(B\x1b[m\x1b[39;49m\x1b[1m  16086.0 \x1b(B\x1b[m\x1b[39;4'
            b'9mtotal,\x1b(B\x1b[m\x1b[39;49m\x1b[1m  15546.2 \x1b(B\x1b[m\x1b[39;4'
            b'9mfree,\x1b(B\x1b[m\x1b[39;49m\x1b[1m    538.7 \x1b(B\x1b[m\x1b[39;49'
            b'mused,\x1b(B\x1b[m\x1b[39;49m\x1b[1m    2\x1b(B\x1b[m\x1b[39;49m\x1b('
            b'B\x1b[m\x1b[39;49m\x1b[K\r\nMiB Swap:\x1b(B\x1b[m\x1b[39;49m\x1b[1m  '
            b'    0.0 \x1b(B\x1b[m\x1b[39;49mtotal,\x1b(B\x1b[m\x1b[39;49m\x1b[1m  '
            b'    0.0 \x1b(B\x1b[m\x1b[39;49mfree,\x1b(B\x1b[m\x1b[39;49m\x1b[1m   '
            b'   0.0 \x1b(B\x1b[m\x1b[39;49mused.\x1b(B\x1b[m\x1b[39;49m\x1b[1m  15'
            b'5\x1b(B\x1b[m\x1b[39;49m\x1b(B\x1b[m\x1b[39;49m\x1b[K\r\n\x1b[K\r\n'
            b'\x1b[7m  PID USER      PR  NI    VIRT    RES    SHR S  %CPU  %MEM '
            b'\x1b(B\x1b[m\x1b[39;49m\x1b[K\r\n\x1b(B\x1b[m    1 root      20   0  '
            b' 28668   6700   6152 S   0.0   0.0 \x1b(B\x1b[m\x1b[39;49m\x1b[K\r\n'
            b'\x1b(B\x1b[m    2 root      20   0       0      0      0 S   0.0   0.'
            b'0 \x1b(B\x1b[m\x1b[39;49m\x1b[K\r\n\x1b(B\x1b[m    3 root      20   0'
            b'       0      0      0 S   0.0   0.0 \x1b(B\x1b[m\x1b[39;49m\x1b[K\r'
            b'\n\x1b(B\x1b[m    4 root       0 -20       0      0      0 I   0.0   '
            b'0.0 \x1b(B\x1b[m\x1b[39;49m\x1b[K\r\n\x1b(B\x1b[m    5 root       0 -'
            b'20       0      0      0 I   0.0   0.0 \x1b(B\x1b[m\x1b[39;49m\x1b[K'
            b'\x1b[H\x1b(B\x1b[mtop - 17:55:19 up 29 min,  0 user,  load average: 0'
            b'.05, 0.07\x1b(B\x1b[m\x1b[39;49m\x1b(B\x1b[m\x1b[39;49m\x1b[K\r\n\r\n'
            b'%Cpu(s):\x1b(B\x1b[m\x1b[39;49m\x1b[1m  0.2 \x1b(B\x1b[m\x1b[39;49mus'
            b',\x1b(B\x1b[m\x1b[39;49m\x1b[1m  0.0 \x1b(B\x1b[m\x1b[39;49msy,\x1b(B'
            b'\x1b[m\x1b[39;49m\x1b[1m  0.0 \x1b(B\x1b[m\x1b[39;49mni,\x1b(B\x1b[m'
            b'\x1b[39;49m\x1b[1m 99.8 \x1b(B\x1b[m\x1b[39;49mid,\x1b(B\x1b[m\x1b[39'
            b';49m\x1b[1m  0.0 \x1b(B\x1b[m\x1b[39;49mwa,\x1b(B\x1b[m\x1b[39;49m'
            b'\x1b[1m  0.0 \x1b(B\x1b[m\x1b[39;49mh\x1b(B\x1b[m\x1b[39;49m\x1b(B'
            b'\x1b[m\x1b[39;49m\x1b[K\r\n\r\n\r\n\x1b[K\r\n\r\n\r\n\r\n\r\n\r\n\x1b'
            b'[?1l\x1b>\x1b[13;1H\r\n\x1b[?12l\x1b[?25h\x1b[K'
        ),
        "running_screen": [
            'top - 17:55:19 up 29 min,  0 user,  load average: 0.05, 0.07',
            'Tasks:  78 total,   1 running,  77 sleeping,   0 stopped,',
            '%Cpu(s):  0.2 us,  0.0 sy,  0.0 ni, 99.8 id,  0.0 wa,  0.0 h',
            'MiB Mem :  16086.0 total,  15546.2 free,    538.7 used,    2',
            'MiB Swap:      0.0 total,      0.0 free,      0.0 used.  155',
            '',
            '  PID USER      PR  NI    VIRT    RES    SHR S  %CPU  %MEM',
            '    1 root      20   0   28668   6700   6152 S   0.0   0.0',
            '    2 root      20   0       0      0      0 S   0.0   0.0',
            '    3 root      20   0       0      0      0 S   0.0   0.0',
            '    4 root       0 -20       0      0      0 I   0.0   0.0',
            '    5 root       0 -20       0      0      0 I   0.0   0.0',
        ],
        "running_cursor": (0, 11),
        "exit_screen": [
            'Tasks:  78 total,   1 running,  77 sleeping,   0 stopped,',
            '%Cpu(s):  0.2 us,  0.0 sy,  0.0 ni, 99.8 id,  0.0 wa,  0.0 h',
            'MiB Mem :  16086.0 total,  15546.2 free,    538.7 used,    2',
            'MiB Swap:      0.0 total,      0.0 free,      0.0 used.  155',
            '',
            '  PID USER      PR  NI    VIRT    RES    SHR S  %CPU  %MEM',
            '    1 root      20   0   28668   6700   6152 S   0.0   0.0',
            '    2 root      20   0       0      0      0 S   0.0   0.0',
            '    3 root      20   0       0      0      0 S   0.0   0.0',
            '    4 root       0 -20       0      0      0 I   0.0   0.0',
            '    5 root       0 -20       0      0      0 I   0.0   0.0',
            '',
        ],
        "exit_cursor": (0, 11),
    },
}
