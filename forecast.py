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
flags.DEFINE_float("battery_efficiency", 96.0, "%")
flags.DEFINE_float("target_reserve", 85.0, "%")
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

  excess_pct = floor((excess_kwh / FLAGS.battery_capacity) * 100)
  target_min = max(FLAGS.min_reserve, FLAGS.target_max - excess_pct - FLAGS.charge_buffer)

  discharge_start_time: Optional[datetime] = None
  first_excess: Optional[int] = None
  target_max_time: Optional[datetime] = None
  clean_backup_time: Optional[datetime] = None

  for idx, fp in enumerate(df.periods.values()):
    if not first_excess and fp.p90_excess_kwh() > 0:
      first_excess = idx
      target_max_time = fp.period_end
    elif first_excess and fp.p90_excess_kwh() == 0:
      clean_backup_time = fp.period_end
      break

  for fp in reversed(list(df.periods.values())[:first_excess]):
    excess_kwh -= fp.p90_avail_kwh()
    if excess_kwh <= 0:
      discharge_start_time = fp.period_end
      break

  return ForecastResult(df.p90_excess_kwh(), discharge_start_time, target_min,
                        target_max_time, clean_backup_time)


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
  for p in df.periods.values():
    if p.p90_kw:
      excess_sum += p.p90_excess_kwh()
      print(f'  {p.period_end.strftime("%Y-%m-%d %H:%M")}, {p.p90_kw:.2f}KW, {p.p90_kwh():.2f}KWh, {p.p90_excess_kwh():.2f}KWh/{excess_sum:.2f}KWh excess')
  print((f'Expected {fr.expected_excess:.2f}KWh excess, '
         f'discharge at {fr.discharge_start_time.strftime("%H:%M")} to {fr.discharge_target:.0f}%, '
         f'reset reserve at {fr.target_reserve_time.strftime("%H:%M")} to {FLAGS.target_max:.0f}%, '
         f'start clean backup at {fr.clean_backup_time.strftime("%H:%M")}'
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
    if fr.expected_excess:
      print_forecast(df, fr)
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
      break


if __name__ == '__main__':
  app.run(main)
