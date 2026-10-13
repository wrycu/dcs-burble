# Burble

Burble is a **complete** [(LSO) ]([LSO](https://en.wikipedia.org/wiki/Landing_signal_officer))for DCS World carrier operations. 
It watches every pass at the boat, talks pilots down live over SRS, grades 
each landing, and keeps a greenie board with a trap card for every pass.

> **Status:** in active development. Expect rough edges.

## What it does

- Live LSO calls over SRS
- Grades landing and uses versions + Tacview recordings to regrade
- Creates trap sheets and greenie boards, optionally posting to Discord
- Gives you analysis of multiple passes, showing what you consistently do right and wrong
- Supports server/client-side captures and Tacview recordings uploaded after-the-fact

How it works
- Reads Tacview's real-time telemetry to avoid DCS-gRPC or mission setup
- Optionally uses a server or client hook to enrich Tacview data
- Provides a hub, with a greenie board and trap sheets
- Stores Tacview recordings for regrading, rewatching, or whatever you want!

## Usage reports

A hub sends anonymous usage counts to the Burble project once a day, so we can see how many hubs run Burble
and how they're used. **It's on unless you turn it off**: on the admin page ("Usage reports") or with
`burble hub telemetry off`. The admin page says so until an admin has chosen, and shows the exact report.

What's sent:
- a random id the hub makes for itself, so its reports can be told apart (not based on its address or anything else)
- the hub's version
- for each of the last 30 days: how many landings, how many pilots landed, how many landings had a server
  report, a pilot's report (pilot hook or uploaded recording) or both, and how many got live LSO calls
- how many pilots landed in the last 30 days
- the server agents used in the last 30 days: how many make live calls over SRS and how many only collect,
  and their versions
- the pilot hooks seen in the last 30 days: how many, and their versions

What's never sent: pilot names, UCIDs or IP addresses; the hub's address or name; mission, server or carrier
names; the tracks, grades or calls themselves, or anything else about a landing.

`burble hub telemetry show` prints the report without sending it. Reports go to `https://telemetry.wrycu.com`
by default; a community can add its own receiver (`burble telemetry serve`, which runs without a hub) or
replace ours, and connect a hub's admin pages to a receiver to see its stats.

## Credits

- [DCS-gRPC/lso](https://github.com/DCS-gRPC/lso): carrier and aircraft reference data (wire
  positions, deck angles, hook offsets) and the deck geometry this project is based on
- [sevenfifty777/DCS-gRPC-lso](https://github.com/sevenfifty777/DCS-gRPC-lso): the gate-based grading approach
- [YoloWingPixie/lsobot](https://github.com/YoloWingPixie/lsobot): handling of DCS's LSO grade strings
- [Tacview](https://www.tacview.net/) for the telemetry, and
  [SRS](https://github.com/ciribob/DCS-SimpleRadioStandalone) for the radios

## License

GPL-3.0. See [LICENSE](LICENSE)
