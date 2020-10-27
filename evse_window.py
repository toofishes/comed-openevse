#!/usr/bin/env python3

import argparse
from datetime import date, datetime, time, timedelta
import re
from typing import Dict, List, Mapping, Tuple

import requests


RatesType = List[Tuple[datetime, float]]
StartEndType = Tuple[datetime, datetime]

def fetch_for_date(session: requests.Session, when: date) -> RatesType:
    """Fetch the ComEd day-ahead hourly prices for the given date."""
    # curl 'https://hourlypricing.comed.com/rrtp/ServletFeed?type=daynexttoday&date=20200726'
    url = "https://hourlypricing.comed.com/rrtp/ServletFeed"
    params = {"type": "daynexttoday", "date": when.strftime("%Y%m%d")}
    response = session.get(url, params=params)
    response.raise_for_status()
    txt = response.text
    if len(txt) == 0:
        raise Exception("empty prices response")

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
        rates.append((parsed_time, float(val.group('rate'))))

    return rates

def fetch_rates(session: requests.Session, second_day: date) -> RatesType:
    """Fetch two days worth of rates and retain the values from 5 PM until 5 PM the second day."""
    rates_a = fetch_for_date(session, second_day - timedelta(days=1))
    rates_b = fetch_for_date(session, second_day)

    # TODO: hardcoded assumption we run this in the 5 PM hour
    cutoff = time.fromisoformat("18:00")
    rates_a = [r for r in rates_a if r[0].time() >= cutoff]
    rates_b = [r for r in rates_b if r[0].time() < cutoff]
    rates = rates_a + rates_b

    return rates

def convert_rates(rates: RatesType) -> RatesType:
    """Converts 'hour ending' rates as returned by the hourly pricing API into
    a per-minute timeset with the expected rate for each particular minute."""
    new_rates: RatesType = []
    for end_hour, price in rates:
        start_hour = end_hour - timedelta(hours=1)
        new_rates.extend([(start_hour + timedelta(minutes=offset), price) for offset in range(60)])

    return new_rates

def find_optimal_window(rates: RatesType, charge_hours: float, awake_until: time) -> StartEndType:
    """Calculate a start and end time to allow charging to occur. The first
    priority is ensuring we have the lowest possible cost window of at least
    `charge_hours`. We then extend the window's end time to `awake_until` if it
    was scheduled to end earlier. This allows things like car preheat/precool
    to be able to draw power from the EVSE."""
    charge_minutes = round(charge_hours * 60)
    rates = convert_rates(rates)
    # sliding windows approach to minimizing cost; find the lowest cost
    # window of the proper length in the data set.
    windows = [0.0] * (len(rates) - charge_minutes + 1)
    for i in range(len(windows)):
        # multiply rates by 10 to avoid floating point rounding errors
        windows[i] = sum(r[1] * 10 for r in rates[i:i+charge_minutes])

    start_idx = min(range(len(windows)), key=windows.__getitem__)
    end_idx = start_idx + charge_minutes

    start = rates[start_idx][0]
    end = rates[end_idx][0]

    # one minute padding to ensure we don't start or end in wrong hour
    # if EVSE and electric meter clocks do not exactly match up
    pad = timedelta(minutes=1)
    start += pad
    end -= pad

    # adjust if necessary for staying awake until a given time
    if awake_until is not None and end.time() < awake_until:
        end = datetime.combine(end.date(), awake_until)

    return start, end

class RAPI:
    """Communicate with OpenEVSE using RAPI over HTTP."""
    def __init__(self, session: requests.Session, url: str):
        self.session = session
        self.url = url

    @staticmethod
    def checksum(cmd: str) -> str:
        """Returns calculated checksum for given RAPI command."""
        cksum = 0
        for char in cmd:
            cksum ^= ord(char)
        return hex(cksum)[2:].upper()

    def cmd_with_checksum(self, cmd: str) -> str:
        """Returns RAPI command with calculated appended checksum."""
        checksum = self.checksum(cmd)
        return f"{cmd}^{checksum}"

    def execute_cmd(self, cmd: str) -> Mapping[str, str]:
        """Executes an RAPI command and returns the parsed JSON response."""
        params = {"json": 1, "rapi": self.cmd_with_checksum(cmd)}
        response = self.session.get(self.url, params=params)
        response.raise_for_status()
        parsed: Dict[str, str] = response.json()
        ret = parsed['ret'].split('^')
        expected_cksum = self.checksum(ret[0])
        if ret[1] != expected_cksum:
            raise Exception(f"mismatched checksum: expected {expected_cksum}, got {ret[1]}")
        parsed['ret_value'] = ret[0]
        parsed['ret_cksum'] = ret[1]
        return parsed

    def set_schedule(self, start: datetime, end: datetime) -> None:
        """Set the delay timer schedule to the given start and end time."""
        response = self.execute_cmd("$GD")
        expected = f"$OK {start.hour} {start.minute} {end.hour} {end.minute}"
        if response['ret_value'] == expected:
            print("Skipping schedule update, no change:", response)
        else:
            cmd = f"$ST {start.hour} {start.minute} {end.hour} {end.minute}"
            response = self.execute_cmd(cmd)
            print("RAPI response:", response)

def main() -> None:
    """Parse arguments, execute the scheduler, and update the charger."""
    parser = argparse.ArgumentParser(
        description="Set OpenEVSE charge timer based on ComEd day ahead pricing")
    parser.add_argument("--hours", type=float, default=4,
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
