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

## Credits

- [DCS-gRPC/lso](https://github.com/DCS-gRPC/lso): carrier and aircraft reference data (wire
  positions, deck angles, hook offsets) and the deck geometry this project is based on
- [sevenfifty777/DCS-gRPC-lso](https://github.com/sevenfifty777/DCS-gRPC-lso): the gate-based grading approach
- [YoloWingPixie/lsobot](https://github.com/YoloWingPixie/lsobot): handling of DCS's LSO grade strings
- [Tacview](https://www.tacview.net/) for the telemetry, and
  [SRS](https://github.com/ciribob/DCS-SimpleRadioStandalone) for the radios

## License

GPL-3.0. See [LICENSE](LICENSE)
