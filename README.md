# dcs-lso

An [LSO](https://en.wikipedia.org/wiki/Landing_signal_officer) for 
[DCS World](https://www.digitalcombatsimulator.com/en/) carrier landings. It watches every pass at the carrier,
talks pilots down over SRS in real time, grades each landing, and keeps a greenie board with a trap
card for every pass.

It uses **Tacview's** real-time telemetry as its data source. You don't need DCS-gRPC,
and pilots don't need to install anything. An optional DCS hook (a small Lua script in
`Scripts/Hooks`) adds what Tacview can't see: DCS's own grade and wire, wind, side numbers and
liveries, and each carrier's radio frequency.

> **Status:** in active development. Expect rough edges.

## What it does

- **Live LSO calls over SRS.**
  - **Calls in the groove:** mimics the built-in LSO calls in DCS
  - **Wave-offs:** for an unsafe pass, a foul deck, or the gear up
  - **After the pass:** "bolter", and "welcome aboard"
  - **Busy groove:** when more than one jet is in the groove, calls are prefixed with the side number
- **Grading**
  - **The grade:** Grading based on the built-in LSO rules in DCS
  - **Regrading:** grading is versioned, so stored passes are regraded when the rules are improved
- **Greenie board and trap cards** (web)
  - **The board:** your classic [greenie board](https://www.airwarriors.com/community/threads/greenie-boards-does-this-one-reflect-a-current-board-and-setup.33928/), automatically maintained and available over the web
  - **Trap cards:** glideslope and lineup plots colored by AOA. Includes LSO callouts made during approach!
  - **Pilot pages:** recurring themes over the last passes ("lined up left at the ramp") and all traps overlaid
- **Multiple ways to import data**
  - **Sources:** supports a server-side collector (zero mission and pilot setup!), a client-side collector (for servers not using the bot), and uploading Tacview recordings
  - **Merging:** reports of the same landing merge into one, graded from the most detailed track (a
    pilot's own recording has their real AOA and twice the sample rate)
- **Uploads**
  - **No account needed:** anyone can upload a Tacview recording and import their own passes from it
  - **Optional password:** pilots can set one so nobody else can upload as them (communities can configure this)


## Credits

- [DCS-gRPC/lso](https://github.com/DCS-gRPC/lso): carrier and aircraft reference data (wire
  positions, deck angles, hook offsets) and the deck geometry this project is based on
- [sevenfifty777/DCS-gRPC-lso](https://github.com/sevenfifty777/DCS-gRPC-lso): the gate-based grading approach
- [YoloWingPixie/lsobot](https://github.com/YoloWingPixie/lsobot): handling of DCS's LSO grade strings
- [Tacview](https://www.tacview.net/) for the telemetry, and
  [SRS](https://github.com/ciribob/DCS-SimpleRadioStandalone) for the radios

## License

GPL-3.0. See [LICENSE](LICENSE)
