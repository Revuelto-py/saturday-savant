"""Self-check for main._wp_minutes, which places each win-probability sample at
its play's game minute (ESPN's college series has no clock of its own).
Needs .env (main connects its DB pool on import): python3 tools/check_wp_minutes.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import main  # noqa: E402

plays = {'1': {'period': 1, 'clock': '15:00'}, '2': {'period': 1, 'clock': '7:30'},
         '3': {'period': 2, 'clock': '9:31'}, '5': {'period': 4, 'clock': '0:00'},
         '6': {'period': 5, 'clock': ''}, '7': {'period': 5, 'clock': ''}, '8': {'period': 2, 'clock': '0:00'}}
wp = [{'playId': i} for i in ('1', '2', '3', 'x', '5', '6', '7')]
x = main._wp_minutes(wp, plays)
assert x[:3] == [0.0, 7.5, round(15 + 15 - (9 + 31 / 60), 3)], x
assert x[3] == 60.0 and x[4] == 60.0, x            # no play: takes the next placed minute
assert x[5:] == [61.5, 63.0], x                     # overtime follows regulation
# a live game in the 2nd quarter ends mid-axis instead of at 60
live = main._wp_minutes([{'playId': '1'}, {'playId': '2'}, {'playId': '3'}], plays)
assert live[-1] < 25, live
# ESPN's secondsLeft wins when sent; a clock never runs backwards
assert main._wp_minutes([{'secondsLeft': 1800}, {'playId': '2'}], plays) == [30.0, 30.0]
print('wp minutes: ok')
