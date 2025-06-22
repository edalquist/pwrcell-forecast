from __future__ import annotations
from absl import app
from absl import flags
from dataclasses import dataclass
from datetime import date, datetime, timedelta, time
from isodate import parse_datetime, parse_duration
from pathlib import Path
from typing import Optional
import isodate
import json
import requests
import tzlocal
from math import floor
import logging
import sys

FLAGS = flags.FLAGS

ONE_HOUR = timedelta(hours=1)
SOLCAST_URL_TEMPLATE = "https://api.solcast.com.au/rooftop_sites/{site_id}/forecasts?format=json&api_key={api_key}"
HA_DATETIME = '%Y-%m-%d %H:%M:%S'

flags.DEFINE_list(
  "files", None, "List of files to use instead of fetching")

flags.DEFINE_list(
  "solcast_sites", None, "List of solcast.com.au site IDs to get forecast data from")
flags.DEFINE_string("solcast_apikey", None, "solcast.com.au API Key")

flags.DEFINE_string("ha_url", None, "Home Assistant Base URL")
flags.DEFINE_string("ha_apikey", None, "Home Assistant API Key")

flags.DEFINE_float("battery_capacity", 17.1, "KWh")
flags.DEFINE_float("inverter_capacity_dc", 8.3, "KW")
flags.DEFINE_float("target_max", 90.0, "%")
flags.DEFINE_float("min_reserve", 10.0, "%")
flags.DEFINE_float("charge_buffer", 10.0, "%")


root = logging.getLogger()
root.setLevel(logging.DEBUG)

handler = logging.StreamHandler(sys.stderr)
handler.setLevel(logging.DEBUG)
formatter = logging.Formatter(
  '%(asctime)s - %(name)s - %(levelname)s - %(message)s')
handler.setFormatter(formatter)
root.addHandler(handler)

requests_log = logging.getLogger("requests.packages.urllib3")
requests_log.setLevel(logging.DEBUG)
requests_log.propagate = True


# These two lines enable debugging at httplib level (requests->urllib3->http.client)
# You will see the REQUEST, including HEADERS and DATA, and RESPONSE with HEADERS but without DATA.
# The only thing missing will be the response.body which is not logged.
# try:
#   import http.client as http_client
# except ImportError:
#   # Python 2
#   import httplib as http_client
# http_client.HTTPConnection.debuglevel = 1


@dataclass
class ForecastResult:
  expected_excess: float
  discharge_start_time: datetime
  discharge_target: float
  target_reserve_time: datetime
  clean_backup_time: datetime


@dataclass
class ForecastPeriod:
  period_end: datetime
  period: timedelta
  p10_kw: float
  p50_kw: float
  p90_kw: float

  def merge(self, other: ForecastPeriod) -> ForecastPeriod:
    if (self == other):
      # Skip merging with self
      return self

    if self.period_end != other.period_end or self.period != other.period:
      raise Exception("period_end or period do not match")

    self.p10_kw += other.p10_kw
    self.p50_kw += other.p50_kw
    self.p90_kw += other.p90_kw
    return self

  def _hour_fraction(self) -> float:
    return self.period.total_seconds() / ONE_HOUR.total_seconds()

  def p90_kwh(self) -> float:
    return self.p90_kw * self._hour_fraction()

  def p90_excess_kwh(self) -> float:
    return max(0, self.p90_kw - FLAGS.inverter_capacity_dc) * self._hour_fraction()

  def p90_avail_kwh(self) -> float:
    return max(0, FLAGS.inverter_capacity_dc - self.p90_kw) * self._hour_fraction()


@dataclass
class DailyForecast:
  period_date: date
  periods: dict[datetime, ForecastPeriod]

  def p10_excess_kwh(self) -> float:
    return sum(fp.p10_excess_kwh() for fp in self.periods.values())

  def p50_excess_kwh(self) -> float:
    return sum(fp.p50_excess_kwh() for fp in self.periods.values())

  def p90_excess_kwh(self) -> float:
    return sum(fp.p90_excess_kwh() for fp in self.periods.values())


def read_to_json(f_name: str):
  with open(f_name) as f:
    return json.load(f)


def merge_forecasts(forecasts: list) -> dict[date, DailyForecast]:
  result: dict[date, DailyForecast] = {}
  for forecast in forecasts:
    for entry in forecast["forecasts"]:
      fp = ForecastPeriod(parse_datetime(entry["period_end"]).astimezone(tz=tzlocal.get_localzone()),
                          parse_duration(entry["period"]),
                          entry["pv_estimate10"],
                          entry["pv_estimate"],
                          entry["pv_estimate90"])
      df = result.get(fp.period_end.date())
      if not df:
        df = DailyForecast(fp.period_end.date(), {})
        result[fp.period_end.date()] = df
      df.periods.setdefault(fp.period_end, fp).merge(fp)

  return result


def get_charge_plan(df: DailyForecast) -> ForecastResult:
  excess_kwh = df.p90_excess_kwh()
  if not excess_kwh:
    return ForecastResult(0, None, None, None, None)

  # Determine the target state of charge to discharge to.
  # We want to end up at target_max after charging the excess, minus a buffer.
  excess_pct = (excess_kwh / FLAGS.battery_capacity) * 100

  # The target minimum charge level we need to reach before the sun comes up.
  # This is capped by the absolute min_reserve.
  target_discharge_pct = max(
      FLAGS.min_reserve, FLAGS.target_max - excess_pct - FLAGS.charge_buffer)

  # Amount of energy to discharge from the battery to reach the target.
  # Assumes we start at target_max.
  kwh_to_discharge = ((FLAGS.target_max - target_discharge_pct) / 100.0) * \
      FLAGS.battery_capacity

  discharge_start_time: Optional[datetime] = None
  first_excess_idx: Optional[int] = None
  target_reserve_time: Optional[datetime] = None
  clean_backup_time: Optional[datetime] = None

  sorted_periods = sorted(df.periods.values(), key=lambda p: p.period_end)

  for idx, fp in enumerate(sorted_periods):
    if first_excess_idx is None and fp.p90_excess_kwh() > 0:
      first_excess_idx = idx
      target_reserve_time = fp.period_end
    elif first_excess_idx is not None and fp.p90_excess_kwh() == 0:
      # This is the first period *after* the excess block ends.
      clean_backup_time = fp.period_end
      break

  if first_excess_idx is None:
    # This should not be reached due to the early return, but is a safeguard.
    return ForecastResult(excess_kwh, None, target_discharge_pct, None, None)

  # Work backwards from when excess starts, to see when we need to start
  # discharging.
  kwh_discharged = 0
  # Look at periods before the first excess period.
  for fp in reversed(sorted_periods[:first_excess_idx]):
    # p90_avail_kwh is the inverter capacity not used by solar, so it's
    # available for battery discharge.
    kwh_discharged += fp.p90_avail_kwh()
    if kwh_discharged >= kwh_to_discharge:
      discharge_start_time = fp.period_end - fp.period  # Start of the period
      break

  now = datetime.now(tz=tzlocal.get_localzone())
  if discharge_start_time and discharge_start_time < now:
    discharge_start_time = now + timedelta(minutes=5)

  return ForecastResult(excess_kwh, discharge_start_time, target_discharge_pct,
                        target_reserve_time, clean_backup_time)


def update_ha(url: str, data: dict) -> requests.Response:
  return requests.post(url,
                       headers={'Authorization': f'Bearer {FLAGS.ha_apikey}'},
                       data=json.dumps(data))


def update_ha_datetime(entity_id: str, dt: datetime) -> None:
  resp = update_ha(f'{FLAGS.ha_url}/api/services/input_datetime/set_datetime',
                   data={
                       'entity_id': f'input_datetime.{entity_id}',
                       'datetime': dt.strftime(HA_DATETIME)
                   })
  if resp.status_code != 200:
    print(f'Error updating {entity_id} to {dt}: {resp}')


def update_ha_number(entity_id: str, val: float) -> None:
  resp = update_ha(f'{FLAGS.ha_url}/api/services/input_number/set_value',
                   data={
                       'entity_id': f'input_number.{entity_id}',
                       'value': val
                   })
  if resp.status_code != 200:
    print(f'Error updating {entity_id} to {val}: {resp}')

def print_forecast(df: DailyForecast, fr: ForecastResult) -> None:
  print(f'Forecast for: {df.period_date.strftime("%Y-%m-%d")}')
  excess_sum: float = 0
  sorted_periods = sorted(df.periods.values(), key=lambda p: p.period_end)
  for p in sorted_periods:
    if p.p90_kw:
      excess_sum += p.p90_excess_kwh()
      print(
          f'  {p.period_end.strftime("%Y-%m-%d %H:%M")}, {p.p90_kw:.2f}KW, '
          f'{p.p90_kwh():.2f}KWh, {p.p90_excess_kwh():.2f}KWh/{excess_sum:.2f}KWh excess'
      )

  def time_str(dt: Optional[datetime]) -> str:
    return dt.strftime("%H:%M") if dt else "N/A"

  print((f'Expected {fr.expected_excess:.2f}KWh excess, '
         f'discharge at {time_str(fr.discharge_start_time)} to {fr.discharge_target:.0f}%, '
         f'reset reserve at {time_str(fr.target_reserve_time)} to {FLAGS.target_max:.0f}%, '
         f'start clean backup at {time_str(fr.clean_backup_time)}'
         ))


def main(argv):
  print("Files: ", FLAGS.files)
  print("Solcast Sites: ", FLAGS.solcast_sites)
  print("Solcast API Key: ", FLAGS.solcast_apikey)

  forecast_json = []
  if FLAGS.files:
    for f in FLAGS.files:
      forecast_json.append(read_to_json(f))
  elif FLAGS.solcast_sites and FLAGS.solcast_apikey:
    cache_dir = Path('.') / 'cache'
    cache_dir.mkdir(exist_ok=True, parents=True)
    for s in FLAGS.solcast_sites:
      now = datetime.now()
      now = datetime(now.year, now.month, now.day, floor(now.hour / 4) * 4)
      cache_file = cache_dir / f'{now.strftime("%Y%m%d%H")}_{s}.json'
      if cache_file.exists():
        print(f'Reading from cache {cache_file}')
        forecast_json.append(read_to_json(cache_file))
      else:
        print(f'Fetching new forecast into {cache_file}')
        with cache_file.open('w') as f:
          api_url = SOLCAST_URL_TEMPLATE.format(
            site_id=s, api_key=FLAGS.solcast_apikey)
          resp = requests.get(api_url)
          if resp.status_code != 200:
            print(f'Failed to fetch {api_url}: {resp.status_code}')
            print(resp.text)
            return
          f.write(resp.text)
          forecast_json.append(resp.json())
  else:
    print("Files must be specified")
    return

  forecasts = merge_forecasts(forecast_json)

  for df in sorted(forecasts.values(), key=lambda df: df.period_date):
    fr = get_charge_plan(df)
    if not fr.expected_excess:
      print(f'Forecast for: {df.period_date.strftime("%Y-%m-%d")} has no excess generation')
    else:
      print_forecast(df, fr)
      if FLAGS.ha_url:
        if (fr.discharge_start_time and fr.target_reserve_time and
            fr.clean_backup_time and fr.discharge_target is not None):
          update_ha_datetime('pwrcell_forecast_discharge_start',
                             fr.discharge_start_time)
          update_ha_datetime('pwrcell_forecast_max_reserve_start',
                             fr.target_reserve_time)
          update_ha_datetime(
              'pwrcell_forecast_clean_backup_start', fr.clean_backup_time)
          update_ha_number('pwrcell_forecast_discharge_target',
                           round(fr.discharge_target))
          update_ha_number('pwrcell_forecast_max_reserve_target',
                           round(FLAGS.target_max))
        else:
          print("Partial forecast result, not updating Home Assistant.")
      break


if __name__ == '__main__':
  app.run(main)
