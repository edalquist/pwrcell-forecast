<!-- @format -->

# Solar Forecast Battery Optimizer

This solves a very specific problem where the solar array I have installed can output more power
than the [Generac PWRCell](https://www.generac.com/all-products/clean-energy/pwrcell) Inverter I
have can convert to AC. However since the system has a DC coupled battery it can shunt that
excess solar power to charge the battery. I want to keep the battery as full as possible though so
that in the event of a power outage I get as much run time as possible.

# How it works

The script is told your SolCast forecast IDs, Inverter Capacity, Battery Capacity, and min/max
charge targets.

When run it calculates the excess power (KWh) estimated to be generated for each 30 minute interval
returned by SolCast. From that it works backwards to determine what time the system should be set
to Sell and what percent the Min Reserve battery parameter should be set to. It also calculates when
the system should be set back to Clean Backup and the Min Reserve increased.

In the ideal situation this will result in your battery discharging to the target % right about the
time that your solar array is approaching the inverter's max capacity, which will result in the
battery starting to recharge.

# Usage

## 1. Solar Forecasting from SolCast

Setup your array or arrays (you can have 2 free) on https://solcast.com.au/ and note down the site
IDs and API key. The API can only be called a few times per day so the script caches the result of
each call for 4 hours.

## 2. Set Your System Parameters

All of the inputs can be specified as flags.

```
python forecast.py --help

       USAGE: forecast.py [flags]
flags:

forecast.py:
  --battery_capacity: KWh
    (default: '17.1')
    (a number)
  --charge_buffer: %
    (default: '10.0')
    (a number)
  --files: List of files to use instead of fetching
    (a comma separated list)
  --ha_apikey: Home Assistant API Key
  --ha_url: Home Assistant Base URL
  --inverter_capacity_dc: KW
    (default: '8.3')
    (a number)
  --min_reserve: %
    (default: '10.0')
    (a number)
  --solcast_apikey: solcast.com.au API Key
  --solcast_sites: List of solcast.com.au site IDs to get forecast data from
    (a comma separated list)
  --target_max: %
    (default: '90.0')
    (a number)
```

## 3. (Optional) Setup Home Assistant

The script can update 5 sensors in Home Assistant with its output to enable automations.

Create the following Helpers which the script will update if given the Home Assistant URL and an
API key.

- Date Time
  - `pwrcell_forecast_discharge_start`
  - `pwrcell_forecast_max_reserve_start`
  - `pwrcell_forecast_clean_backup_start`
- Number (percent)
  - `pwrcell_forecast_discharge_target`
  - `pwrcell_forecast_max_reserve_target`
