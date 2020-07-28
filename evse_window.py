#!/usr/bin/env python3

from datetime import date, datetime, time, timedelta
import re
import sys

import requests


# How many hours do we need to fully charge the car? Find the cheapest window
# of time to accomidate this.
CHARGE_HOURS = 4

# What time in the morning should we not sleep before? This keeps the charger
# out of sleep for things like car preheat and precool.
AWAKE_UNTIL = time.fromisoformat("09:30")

# What price to allow charging at regardless of how many hours we need?
ALLOW_CHARGE_PRICE = 1.6

session = requests.Session()

def fetch_for_date(when):
    # curl 'https://hourlypricing.comed.com/rrtp/ServletFeed?type=daynexttomorrow'
    # curl 'https://hourlypricing.comed.com/rrtp/ServletFeed?type=daynexttoday&date=20200726'
    url = "https://hourlypricing.comed.com/rrtp/ServletFeed"
    if when is None:
        params = {"type": "daynexttomorrow"}
    else:
        params = {"type": "daynexttoday", "date": when.strftime("%Y%m%d")}
    req = session.get(url, params=params)
    txt = req.text

    # format: "[[Date.UTC(2020,6,18,0,0,0), 1.8], ...]"
    date_re = re.compile(
        r"\[Date\.UTC\((?P<y>\d+),(?P<m>\d+),(?P<d>\d+),(?P<h>\d+),0,0\), (?P<rate>\d+\.\d+)\]")

    # parse the JS-style date/rate feed
    rates = []
    for val in date_re.finditer(txt):
        parsed_time = datetime(int(val.group('y')), int(val.group('m')) + 1,
                               int(val.group('d')), int(val.group('h')))
        rates.append([parsed_time, float(val.group('rate')) * 10])

    return rates

def fetch_rates():
    if len(sys.argv) > 1:
        day = date.fromisoformat(sys.argv[1])
        rates_a = fetch_for_date(day - timedelta(days=1))
        rates_b = fetch_for_date(day)
    else:
        rates_a = fetch_for_date(date.today())
        rates_b = fetch_for_date(None)

    # TODO: hardcoded assumption we run this in the 5 PM hour
    cutoff = time.fromisoformat("18:00")
    rates_a = [r for r in rates_a if r[0].time() >= cutoff]
    rates_b = [r for r in rates_b if r[0].time() < cutoff]
    rates = rates_a + rates_b

    return rates

def find_optimal_window(rates):
    # sliding windows approach to minimizing cost; find the lowest cost
    # window of CHARGE_HOURS length in the data set.
    windows = [None] * (len(rates) - CHARGE_HOURS)
    for i in range(len(windows)):
        windows[i] = sum(r[1] for r in rates[i:i+CHARGE_HOURS])

    start_idx = min(range(len(windows)), key=windows.__getitem__)
    end_idx = start_idx + CHARGE_HOURS

    # expand window for all nearby hours under our maximum cost
    max_rate = ALLOW_CHARGE_PRICE * 10
    while start_idx > 0 and rates[start_idx - 1][1] < max_rate:
        start_idx -= 1
    while end_idx < len(rates) - 1 and rates[end_idx + 1][1] < max_rate:
        end_idx += 1

    # rates are listed as "hour ending", so start time is 1 hour before
    start = rates[start_idx][0] - timedelta(hours=1)
    end = rates[end_idx][0] - timedelta(hours=1)

    # adjust if necessary for comfort
    if end.time() < AWAKE_UNTIL:
        end = datetime.combine(end.date(), AWAKE_UNTIL)

    return start, end

def update_charger(start, end):
    # pad window to make sure we don't start or end in wrong hour
    start += timedelta(minutes=2)
    end -= timedelta(minutes=2)
    params = {"json": 1, "rapi": f"$ST {start.hour} {start.minute} {end.hour} {end.minute}"}
    print(params)
    request = session.get("http://openevse-xxxx/r", params=params)
    print(request.text)

def main():
    rates = fetch_rates()
    start, end = find_optimal_window(rates)
    print(f"Time window: {start} {end}")
    # only update charger if we are querying today
    if len(sys.argv) == 1:
        update_charger(start, end)


if __name__ == '__main__':
    main()
