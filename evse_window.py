#!/usr/bin/env python3

import argparse
from datetime import date, datetime, time, timedelta
import re

import requests


def fetch_for_date(session, when):
    """Fetch the ComEd day-ahead hourly prices for the given date."""
    # curl 'https://hourlypricing.comed.com/rrtp/ServletFeed?type=daynexttoday&date=20200726'
    url = "https://hourlypricing.comed.com/rrtp/ServletFeed"
    params = {"type": "daynexttoday", "date": when.strftime("%Y%m%d")}
    req = session.get(url, params=params)
    txt = req.text

    # format: "[[Date.UTC(2020,6,18,0,0,0), 1.8], ...]"
    # note that they aren't UTC at all, they are America/Chicago TZ
    date_re = re.compile(
        r"\[Date\.UTC\((?P<y>\d+),(?P<m>\d+),(?P<d>\d+),(?P<h>\d+),0,0\), (?P<rate>\d+\.\d+)\]")

    # parse the JS-style date/rate feed
    rates = []
    for val in date_re.finditer(txt):
        # JS dates use 0-indexed months, thus the `+ 1`
        parsed_time = datetime(int(val.group('y')), int(val.group('m')) + 1,
                               int(val.group('d')), int(val.group('h')))
        rates.append([parsed_time, float(val.group('rate'))])

    return rates

def fetch_rates(session, second_day):
    """Fetch two days worth of rates and retain the values from 5 PM until 5 PM the second day."""
    rates_a = fetch_for_date(session, second_day - timedelta(days=1))
    rates_b = fetch_for_date(session, second_day)

    # TODO: hardcoded assumption we run this in the 5 PM hour
    cutoff = time.fromisoformat("18:00")
    rates_a = [r for r in rates_a if r[0].time() >= cutoff]
    rates_b = [r for r in rates_b if r[0].time() < cutoff]
    rates = rates_a + rates_b

    return rates

def find_optimal_window(rates, charge_hours, awake_until):
    """Calculate a start and end time to allow charging to occur. The first
    priority is ensuring we have the lowest possible cost window of at least
    `charge_hours`. We then extend the window's end time to `awake_until` if it
    was scheduled to end earlier. This allows things like car preheat/precool
    to be able to draw power from the EVSE."""
    # sliding windows approach to minimizing cost; find the lowest cost
    # window of the proper length in the data set.
    windows = [None] * (len(rates) - charge_hours + 1)
    for i in range(len(windows)):
        # multiply rates by 10 to avoid floating point rounding errors
        windows[i] = sum(r[1] * 10 for r in rates[i:i+charge_hours])

    start_idx = min(range(len(windows)), key=windows.__getitem__)
    end_idx = start_idx + charge_hours - 1

    # rates are listed as "hour ending", so start time is 1 hour before
    start = rates[start_idx][0] - timedelta(hours=1)
    end = rates[end_idx][0]

    # pad window to make sure we don't start or end in wrong hour
    pad = timedelta(minutes=2)
    start += pad
    end -= pad

    # adjust if necessary for staying awake until a given time
    if awake_until is not None and end.time() < awake_until:
        end = datetime.combine(end.date(), awake_until)

    return start, end

class RAPI:
    """Communicate with OpenEVSE using RAPI over HTTP."""
    def __init__(self, session, url):
        self.session = session
        self.url = url

    @staticmethod
    def cmd_with_checksum(cmd):
        """Returns RAPI command with calculated appended checksum."""
        cksum = 0
        for char in cmd:
            cksum ^= ord(char)
        return f"{cmd}^{hex(cksum)[2:]}"

    def execute_cmd(self, cmd):
        """Executes an RAPI command and returns the parsed JSON response."""
        params = {"json": 1, "rapi": self.cmd_with_checksum(cmd)}
        response = self.session.get(self.url, params=params)
        return response.json()

    def set_schedule(self, start, end):
        """Set the delay timer schedule to the given start and end time."""
        response = self.execute_cmd("$GD")
        expected = f"$OK {start.hour} {start.minute} {end.hour} {end.minute}"
        if response['ret'].split('^')[0] == expected:
            print("Skipping schedule update, no change:", response)
        else:
            cmd = f"$ST {start.hour} {start.minute} {end.hour} {end.minute}"
            response = self.execute_cmd(cmd)
            print("RAPI response:", response)

def main():
    """Parse arguments, execute the scheduler, and update the charger."""
    parser = argparse.ArgumentParser(
        description="Set OpenEVSE charge timer based on ComEd day ahead pricing")
    parser.add_argument("--hours", type=int, default=4,
                        help="find charge window of at this many hours (default: %(default)s)")
    parser.add_argument("--awake-until", metavar='TIME', type=time.fromisoformat,
                        help="regardless of charge window length, don't sleep until this time")
    parser.add_argument("--rapi-url", metavar='URL', default="http://openevse.local/r",
                        help="full URL to make an RAPI API call (default: %(default)s)")
    default_date = date.today() + timedelta(days=1)
    parser.add_argument("--date", type=date.fromisoformat, default=default_date,
                        help="date to use to find ideal window (default: tomorrow %(default)s)")
    args = parser.parse_args()

    session = requests.Session()
    rates = fetch_rates(session, args.date)
    start, end = find_optimal_window(rates, args.hours, args.awake_until)
    print(f"Time window: {start} {end}")
    if args.rapi_url:
        rapi = RAPI(session, args.rapi_url)
        rapi.set_schedule(start, end)


if __name__ == '__main__':
    main()
