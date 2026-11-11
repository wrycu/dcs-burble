# Burble

Burble is a **complete** [(LSO)]([LSO](https://en.wikipedia.org/wiki/Landing_signal_officer)) for DCS World carrier operations. 
It watches every pass at the boat, talks pilots down live over SRS, grades 
each landing, and keeps a greenie board with a trap card for every pass.

## How it's different

- Burble does not require add-ons such as DCS-gRPC or scripts in the mission like MOOSE Airboss, instead relying on Tacview data
- Landings can be captured per-pilot, regardless of which server they fly on
- You can self-host it or modify it to your hearts content
- Provides a re-created LSO PLAT view of passes, both recorded and live

## What it does

- Live LSO calls over SRS
- Grades landing and uses versions + Tacview recordings to regrade
- Creates trap sheets and greenie boards, optionally posting to Discord
- Gives you analysis of multiple passes, showing what you consistently do right and wrong
- Supports server/client-side captures and Tacview recordings uploaded after-the-fact

## What it doesn't do

- Marshaling of recovering aircraft

## How it works

- Reads Tacview's real-time telemetry to avoid DCS-gRPC or mission setup
- Optionally uses a server or client hook to enrich Tacview data
- Provides a hub, with a greenie board and trap sheets
- Stores Tacview recordings for regrading, rewatching, or whatever you want!

## AI Disclosure
I designed this architecture based on my knowledge of DCS 
and the ecosystem. AI has been tasked with implementing specific 
features under my direction. All functionality is thoroughly tested
before any commits are made. 

In short: while AI has been involved in the development of this project,
I do not feel it was "vibe coded." Take that how you will, though!

## Preview

Please excuse poor passes on the carrier. I developed this project largely to help myself (and my friends) get better at this, but it hasn't happened yet.

The trap sheet

<img width="1503" height="1254" alt="image" src="https://github.com/user-attachments/assets/05ce683a-d0fb-4f2a-85f4-87f618e93898" />


The Greenie board

<img width="1280" height="840" alt="board-dark" src="https://github.com/user-attachments/assets/45686d24-7126-419b-8c27-351ed8303529" />

The pilot page
<img width="1280" height="910" alt="pilot-light" src="https://github.com/user-attachments/assets/3909293e-921e-4a08-afa1-31ebff7b0dea" />

## Credits

- [DCS-gRPC/lso](https://github.com/DCS-gRPC/lso): carrier and aircraft reference data (wire
  positions, deck angles, hook offsets) and the deck geometry this project is based on
- [sevenfifty777/DCS-gRPC-lso](https://github.com/sevenfifty777/DCS-gRPC-lso): the gate-based grading approach
- [YoloWingPixie/lsobot](https://github.com/YoloWingPixie/lsobot): handling of DCS's LSO grade strings
- [Tacview](https://www.tacview.net/) for the telemetry, and
  [SRS](https://github.com/ciribob/DCS-SimpleRadioStandalone) for the radios

## Usage reports

Hubs send anonymous usage counts to the Burble project once a day. This can be disabled in the settings. A full detail of what's sent is included in the settings page.

## License

GPL-3.0. See [LICENSE](LICENSE)
