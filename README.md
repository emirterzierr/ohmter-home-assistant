# Ohmter for Home Assistant

A custom integration that lets [Ohmter](https://ohmter.nl) schedule the devices
your Home Assistant already controls into the cheapest hours of your dynamic
energy contract. Status: **rollout** - real code, not yet driven end to end
against a household's real hardware.

Ohmter is not an energy supplier. You keep your contract (Frank Energie,
Tibber, Zonneplan); Ohmter decides *when*.

## How it connects

The integration runs inside your Home Assistant and connects **outward** to
ohmter.nl over HTTPS. Nothing is opened on your router, Ohmter never reaches
into your home, and you do not need Nabu Casa or any remote access.

Pairing takes one code:

1. In the Ohmter dashboard open **Settings → Home Assistant → Pair a Home
   Assistant**. A code like `ABCD-EFGH` appears; it is valid for ten minutes
   and works once.
2. In Home Assistant add the **Ohmter** integration and type the code.

That is all. No Ohmter password, no Home Assistant token and no vendor login
ever leaves your machine. Ohmter issues the integration its own token, stores
only a hash of it, and you can revoke it from either side: **Disconnect** in
the Ohmter dashboard, or delete the integration here.

## What Ohmter sees and does

Only the entities you map, and nothing else. After pairing, open the
integration's **Configure** and add a device:

| Kind | Ohmter reads | Ohmter controls |
|---|---|---|
| EV charger | power (required), energy, plugged-in, switch state | charging on/off - only once you have mapped a switch **and confirmed it** on the check screen; max current - only with a `number` entity mapped. Without those the charger is monitored only, and Ohmter says so |
| Thermostat | current and target temperature, mode | target temperature; restores what you had at the end of a window |
| P1 smart meter | grid import/export | nothing |
| Solar inverter | production | nothing |
| Home battery | state of charge, power | nothing - read only in this version |

For each role the picker shows only plausible entities (a `switch` for the
charging switch, a power `sensor` for power). What your Home Assistant can do,
Ohmter can schedule; Ohmter does not add capabilities your integration lacks.

**Switch off your charger's own smart or eco mode.** Wallbox, Zaptec, go-e and
others ship a scheduler of their own; two plans fighting over one car is worse
than either alone. The bridge reports when the charging switch has gone
unavailable, which is how those modes usually show up.

## If Ohmter goes quiet

The bridge never leaves your hardware in a state Ohmter asked for once Ohmter
has stopped answering. After fifteen minutes without a successful sync it
switches a charger Ohmter had paused back **on**, restores a max current
Ohmter lowered, and puts a thermostat back to what you had. Every command
Ohmter sends carries an expiry, and an expired one is never run.

Three diagnostic entities show what is happening: **Ohmter connected**,
**Last sync with Ohmter** and **Last Ohmter command**.

## Installing

Through [HACS](https://hacs.xyz): add this repository as a custom repository
of type *Integration*, install **Ohmter**, restart Home Assistant. Or copy
`custom_components/ohmter` into your `config/custom_components/` folder.

Requires Home Assistant 2025.6 or newer.

## Privacy

The integration sends the states of the entities you mapped to ohmter.nl
every 20 seconds, together with your Home Assistant version and its instance
id (so two copies of one pairing can be told apart). Nothing else. Ohmter's
privacy policy at https://ohmter.nl/privacy covers this data; **Delete my
account** in the dashboard removes it, the pairing included.

## Trademark

Home Assistant is a trademark of Nabu Casa, Inc. This integration is not
affiliated with, endorsed by or certified by Home Assistant or Nabu Casa; the
name is used only to say what it works with.
