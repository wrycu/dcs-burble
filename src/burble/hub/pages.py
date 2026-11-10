"""HTML pages for the hub: the greenie board and per-pass pages."""

from __future__ import annotations

import json
from collections import defaultdict
from contextvars import ContextVar
from datetime import datetime
from html import escape
from urllib.parse import quote

from ..callouts.voice import EACH_CARRIER
from ..cards.overlay import GRADE_COLORS
from ..cards.theme import COLOR_VARS, FONT_VAR, GRADE_VARS, MAX_CSS, safe_css
from ..geometry import airframe
from ..grading import grade_name, grade_short
from ..weather import fog
from ..telemetry.pages import STYLE as USAGE_STYLE, usage_body
from .accuracy import Accuracy
from .db import Pass, Upload

# Greenie board colours per grade (light, dark), shared with the overlay card.

STYLE = """
/* The Ready Room look (PLAN #63). Built-in values sit in :where(:root) so the admins' custom CSS (:root { --bg: ... })
   always wins; dark mode follows the header's Light/Dark toggle (data-theme on <html>), else the system. */
:where(:root) { color-scheme: light; --bg: #eae8dc; --card: #f8f7ef; --surface2: #e0e6db; --text: #172a2c;
  --muted: #4d6361; --border: #aabbb0; --empty: #e0e6db; --row-alt: #f1f0e6; --header: #152d30;
  --header-text: #eaf0e9; --accent: #245b49; --button: #173a38; --button-text: #ffffff; --paper-button: #fffdf6;
  --paper-button-text: #1b3430; --navy: #203b3e; --danger: #a4473d; --warn: #8a6414; --good: #2f6b45;
  --mono: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  --board-frame: #333f3d; --board-frame-edge: #687972; --board-shadow: #0f1f1e; --board-paper: #eeeade; --board-ink: #1a2d2a; --board-rule: #8c9b90; --board-strip: #d6dacc; --board-strip-ink: #34483f; --board-grid: #a7afa2; --board-th: #e4e5d8; --board-th-ink: #3d5046; --board-name: #183b31; --board-side: #53685e; --board-empty: #e2e4d6; --board-note: #4b6054; --board-note-strong: #193c2d; }
@media (prefers-color-scheme: dark) { :where(:root:not([data-theme="light"])) { color-scheme: dark; --bg: #172324; --card: #243532; --surface2: #2c4039; --text: #e6eee7; --muted: #b1c3b9; --border: #526960; --empty: #304440; --row-alt: #2a3d39; --header: #0e1b1d; --accent: #abd4b4; --button: #d5e5d4; --button-text: #17352c; --paper-button: #304440; --paper-button-text: #edf4ed; --navy: #1c3739; --danger: #e08a7f; --warn: #e0c070; --good: #8fcf9f; --board-frame: #0b1515; --board-frame-edge: #3b4f4b; --board-shadow: #030909; --board-paper: #1c2928; --board-ink: #e2eae3; --board-rule: #435853; --board-strip: #243533; --board-strip-ink: #b4c6bb; --board-grid: #364945; --board-th: #213130; --board-th-ink: #a8bbb0; --board-name: #d3e8d8; --board-side: #94a99e; --board-empty: #2a3b38; --board-note: #a3b6aa; --board-note-strong: #d8e8dc; } }
:where(:root[data-theme="dark"]) { color-scheme: dark; --bg: #172324; --card: #243532; --surface2: #2c4039; --text: #e6eee7; --muted: #b1c3b9; --border: #526960; --empty: #304440; --row-alt: #2a3d39; --header: #0e1b1d; --accent: #abd4b4; --button: #d5e5d4; --button-text: #17352c; --paper-button: #304440; --paper-button-text: #edf4ed; --navy: #1c3739; --danger: #e08a7f; --warn: #e0c070; --good: #8fcf9f; --board-frame: #0b1515; --board-frame-edge: #3b4f4b; --board-shadow: #030909; --board-paper: #1c2928; --board-ink: #e2eae3; --board-rule: #435853; --board-strip: #243533; --board-strip-ink: #b4c6bb; --board-grid: #364945; --board-th: #213130; --board-th-ink: #a8bbb0; --board-name: #d3e8d8; --board-side: #94a99e; --board-empty: #2a3b38; --board-note: #a3b6aa; --board-note-strong: #d8e8dc; }
* { box-sizing: border-box; }
body { margin: 0; min-height: 100vh; display: flex; flex-direction: column; background: var(--bg); color: var(--text); font: 15px/1.5 var(--font, system-ui), -apple-system, "Segoe UI", sans-serif; }
a { color: inherit; }
button, select, input, textarea { font: inherit; }
a:focus-visible, button:focus-visible, input:focus-visible, select:focus-visible, textarea:focus-visible,
[tabindex]:focus-visible { outline: 3px solid #db964e; outline-offset: 3px; }
[hidden] { display: none !important; }

/* Header */
header.site { background: var(--header); color: var(--header-text); border-bottom: 3px solid #b5ad8a; }
header.site .top { max-width: 1500px; margin: 0 auto; padding: 14px 40px; display: flex; align-items: center; gap: 32px; }
.brand { display: flex; align-items: center; gap: 12px; white-space: nowrap; text-decoration: none; }
.brandmark { width: 38px; height: 38px; display: grid; place-items: center; border: 1px solid #adc3b5; font: 700 17px Georgia, serif; }
.brandmark.custom { border: 0; overflow: hidden; }
.brandmark.custom img { width: 100%; height: 100%; object-fit: contain; }
.icon-row { display: flex; align-items: center; gap: 14px; flex-wrap: wrap; margin: 8px 0 18px; }
.icon-row .preview { width: 48px; height: 48px; display: grid; place-items: center; background: var(--header);
  color: var(--header-text); font: 700 20px Georgia, serif; flex: none; }
.icon-row .preview img { width: 100%; height: 100%; object-fit: contain; }
.icon-row form { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin: 0; }
.brand strong { display: block; font-size: 14px; letter-spacing: .08em; }
.brand small { display: block; font: 10px var(--mono); letter-spacing: .12em; color: #b6c7b9; margin-top: 3px; }
nav.nav { display: flex; gap: 2px; overflow-x: auto; }
nav.nav a { color: #c4d1c8; text-decoration: none; padding: 10px 13px; font-size: 14px; white-space: nowrap;
  border-bottom: 2px solid transparent; min-height: 44px; display: flex; align-items: center; }
nav.nav a:hover { color: #fff; }
nav.nav a[aria-current=page] { background: #2d4944; color: #fff; border-bottom-color: #d1bd78; }
.header-right { margin-left: auto; display: flex; align-items: center; gap: 8px; white-space: nowrap; }
.header-right form { margin: 0; }
.header-right .simple, .theme-toggle { min-height: 40px; padding: 8px 12px; font-size: 13px; color: #eff4ed;
  background: transparent; border: 1px solid transparent; cursor: pointer; text-decoration: none; display: inline-flex; align-items: center; }
.theme-toggle { border-color: #718a7d; }
.header-right .simple:hover { text-decoration: underline; }
.header-right .me { color: #fff; font-weight: 700; }
.header-right a[aria-current=page] { background: #2d4944; color: #fff; border-bottom: 2px solid #d1bd78; }

/* Buttons */
.button { min-height: 44px; padding: 10px 16px; border: 1px solid var(--border); font-size: 13px; font-weight: 750;
  letter-spacing: .02em; text-decoration: none; display: inline-flex; align-items: center; justify-content: center;
  gap: 6px; cursor: pointer; }
.button.lime { background: #d5e5d4; border-color: #d5e5d4; color: #17352c; }
.button.dark, form.upload button[type=submit], form.filters button[type=submit] { background: var(--button);
  border: 1px solid var(--button); color: var(--button-text); }
.button.paper { background: var(--paper-button); color: var(--paper-button-text); }
.text-link { color: var(--accent); font-weight: 700; font-size: 14px; text-underline-offset: 4px; }

/* Page */
main { flex: 1; width: 100%; max-width: 1500px; margin: 0 auto; padding: 34px 40px 72px; }
h1 { font-size: clamp(30px, 3.1vw, 44px); line-height: 1.08; letter-spacing: -.04em; margin: 8px 0; }
h1 .modex { color: #819b8d; font-weight: 700; margin-left: 6px; }
h2 { font-size: 20px; letter-spacing: -.03em; margin: 24px 0 8px; }
.micro { font: 700 11px/1.4 var(--mono); letter-spacing: .1em; text-transform: uppercase; }
.quiet, .sub { color: var(--muted); }
.sub { margin: 0 0 16px; }
.page-header { display: flex; align-items: end; justify-content: space-between; gap: 20px; margin-bottom: 24px; flex-wrap: wrap; }
.page-header p { margin: 0; color: var(--muted); }
.page-header .actions { display: flex; gap: 9px; flex-wrap: wrap; }
.page-header .micro.under { margin: 2px 0 6px; }
.panel { background: var(--card); border: 1px solid var(--border); padding: 24px; }
.panel + .panel { margin-top: 16px; }
.panel h3 { font-size: 19px; letter-spacing: -.025em; margin: 7px 0; }
.panel > p { color: var(--muted); font-size: 14px; }
.section-header { display: flex; align-items: end; justify-content: space-between; gap: 18px; margin: 36px 0 14px; flex-wrap: wrap; }
.section-header h2 { margin: 4px 0 0; font-size: 23px; }
.section-header h2.section { margin: 0; font-size: 28px; letter-spacing: -.02em; }
.section-header p { color: var(--muted); margin: 0; font-size: 13px; }
.section-header .scope { font-size: 14px; }
.scroll, .scroll-x { overflow-x: auto; }
.empty-state { color: var(--muted); padding: 24px; text-align: center; }
.error { color: var(--danger); }
footer.site { width: 100%; max-width: 1500px; margin: 0 auto; padding: 18px 40px 40px; display: flex; justify-content: space-between;
  gap: 16px; color: var(--muted); font-size: 13px; }
footer.site > div { border-top: 1px solid var(--border); padding-top: 18px; flex: 1; display: flex; justify-content: space-between; gap: 16px; flex-wrap: wrap; }
footer.site a { color: var(--text); font-weight: 700; }

/* Grade squares: one palette in both modes (the record), on light paper or the dark slate */
.cell, .tile, .stamp { color: #fff; text-decoration: none; font: 800 13px var(--mono); }
.cell { display: grid; place-items: center; width: 40px; height: 38px; }
.cell.empty, .tile.empty { background: var(--empty); }
.cell.night, .tile.night { position: relative; }
.cell.night::after, .tile.night::after, .key .night-dot { content: ""; position: absolute; top: 5px; right: 5px; width: 7px; height: 7px;
  border-radius: 50%; background: #111; box-shadow: 0 0 0 1px rgba(255, 255, 255, 0.7); }
.cell:hover, .tile:hover { outline: 3px solid #1d2923; outline-offset: -3px; }
.stamp { width: 58px; height: 58px; display: grid; place-items: center; font-size: 26px; flex: none; }

/* The board: a framed paper record (--board-*: paper in light mode, a dark slate in dark) */
.board-frame { background: var(--board-frame); border: 1px solid var(--board-frame-edge); padding: 10px; box-shadow: 0 4px 0 var(--board-shadow); margin-bottom: 20px; }
.board-paper { background: var(--board-paper); color: var(--board-ink); }
.board-paper .tile:hover { outline-color: var(--board-ink); }
.board-heading { display: flex; justify-content: space-between; gap: 24px; align-items: center; padding: 20px 24px;
  border: 1px solid var(--board-rule); border-bottom: 0; }
.board-heading h2 { margin: 0; font-size: 25px; line-height: 1.15; letter-spacing: .025em; text-transform: uppercase; }
.board-strip { display: flex; justify-content: space-between; gap: 15px; background: var(--board-strip); border: 1px solid var(--board-rule);
  padding: 9px 24px; color: var(--board-strip-ink); font: 700 11px/1.4 var(--mono); letter-spacing: .08em; text-transform: uppercase; }
.board-scroll { overflow-x: auto; border: 1px solid var(--board-rule); border-top: 0; }
table.board { border-collapse: collapse; width: 100%; table-layout: fixed; }
table.board th, table.board td { border-right: 1px solid var(--board-grid); border-bottom: 1px solid var(--board-grid); }
table.board th { background: var(--board-th); color: var(--board-th-ink); font: 700 11px/1.3 var(--mono); letter-spacing: .07em;
  padding: 12px 6px; text-transform: uppercase; }
table.board th.pilot { width: 170px; text-align: left; padding-left: 18px; }
table.board th.num { width: 68px; }
table.board td { padding: 0; text-align: center; }
table.board td.pilot { text-align: left; padding: 14px 18px; overflow-wrap: anywhere; }
table.board td.pilot a { font-size: 18px; color: var(--board-name); font-weight: 800; text-underline-offset: 4px; }
table.board td.pilot .side { display: block; color: var(--board-side); font: 12px var(--mono); margin-top: 3px; }
table.board td.num { font: 700 16px var(--mono); color: var(--board-ink); }
table.board .tile.night::after { top: 50%; left: 50%; right: auto; transform: translate(-50%, -50%); }
table.board .tile { display: grid; place-items: center; min-height: 50px; margin: 4px 3px; font-size: 12px; }
.board-paper .tile.empty { background: var(--board-empty); }
.board-hint { display: none; color: var(--muted); font-size: 13px; margin: -8px 0 16px; }
.board-note { padding: 13px 24px; display: flex; justify-content: space-between; gap: 20px; font-size: 13px; color: var(--board-note); flex-wrap: wrap; }
.board-note strong { color: var(--board-note-strong); }
.key { display: flex; flex-wrap: wrap; gap: 10px 20px; padding: 4px 0 0; color: var(--muted); font-size: 13px; }
.key strong { color: var(--text); }
.key .swatch { display: inline-block; width: 11px; height: 11px; margin-right: 5px; vertical-align: -1px; }
.key .night-dot { position: relative; display: inline-block; top: 0; right: 0; margin-right: 6px; vertical-align: 0; }

/* Filters */
form.filters { display: flex; gap: 10px; align-items: end; flex-wrap: wrap; background: var(--surface2); padding: 16px;
  border: 1px solid var(--border); margin: 0 0 16px; }
form.filters label { display: grid; gap: 6px; min-width: 135px; flex: 1; font: 700 11px var(--mono); letter-spacing: .08em;
  text-transform: uppercase; color: var(--muted); }
form.filters select, form.filters input:not([type=checkbox]) { width: 100%; min-height: 44px; padding: 9px 10px;
  border: 1px solid var(--border); background: var(--paper-button); color: var(--paper-button-text);
  font: 15px var(--font, system-ui), sans-serif; letter-spacing: 0; text-transform: none; }
form.filters label.check { display: flex; align-items: center; gap: 7px; flex: 0 1 auto; padding: 10px 0;
  font: 13px var(--font, system-ui), sans-serif; letter-spacing: 0; text-transform: none; }
form.filters label.check input { width: 17px; height: 17px; accent-color: #407b59; }
form.filters button[type=submit] { min-height: 44px; padding: 10px 18px; font-weight: 750; cursor: pointer; }
form.filters .field { display: grid; gap: 6px; min-width: 135px; flex: 1; font: 700 11px var(--mono);
  letter-spacing: .08em; text-transform: uppercase; color: var(--muted); }
/* The pilot picker: a searchable list of names, any number chosen */
.pick { position: relative; }
.pick summary { list-style: none; min-height: 44px; padding: 11px 30px 9px 10px; border: 1px solid var(--border);
  background: var(--paper-button); color: var(--paper-button-text); font: 15px var(--font, system-ui), sans-serif;
  letter-spacing: 0; text-transform: none; cursor: pointer; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.pick summary::-webkit-details-marker { display: none; }
.pick summary::after { content: "▾"; position: absolute; right: 11px; top: 12px; }
.pick[open] summary::after { content: "▴"; }
.pick .drop { position: absolute; z-index: 5; left: 0; right: 0; min-width: 220px; margin-top: 4px; padding: 8px;
  background: var(--card); border: 1px solid var(--border); box-shadow: 0 8px 24px rgb(0 0 0 / .25); }
.pick .drop input[type=search] { margin-bottom: 6px; }
.pick .names { max-height: 240px; overflow-y: auto; }
form.filters .pick .names label { display: flex; align-items: center; gap: 8px; min-width: 0; padding: 7px 4px;
  font: 14px var(--font, system-ui), sans-serif; letter-spacing: 0; text-transform: none; color: var(--text); cursor: pointer; }
.pick .names label:hover { background: var(--surface2); }
.pick .names input { width: 16px; height: 16px; accent-color: #407b59; }
.pick .none { margin: 6px 4px; color: var(--muted); font: 13px var(--font, system-ui), sans-serif; text-transform: none; letter-spacing: 0; }
.pick .clear { margin-top: 6px; background: none; border: 0; color: var(--text); text-decoration: underline;
  font: 13px var(--font, system-ui), sans-serif; text-transform: none; letter-spacing: 0; cursor: pointer; padding: 4px; }
form.filters.compact { padding: 12px; width: fit-content; max-width: 100%; flex-wrap: nowrap; }
form.filters.compact > .field, form.filters.compact > label { flex: 0 0 auto; min-width: 0; }
form.filters.compact select { width: auto; }
form.filters .stepper input { width: 4.5em; }
form.filters .stepper { display: flex; }
form.filters .stepper input { text-align: center; -moz-appearance: textfield; appearance: textfield; }
form.filters .stepper input::-webkit-inner-spin-button, form.filters .stepper input::-webkit-outer-spin-button {
  -webkit-appearance: none; margin: 0; }
form.filters .stepper button { min-width: 44px; min-height: 44px; border: 1px solid var(--border);
  background: var(--paper-button); color: var(--paper-button-text); font: 700 18px var(--mono); cursor: pointer; }
form.filters .stepper button:first-child { border-right: 0; }
form.filters .stepper button:last-child { border-left: 0; }
form.filters.compact label { flex: 0 1 240px; }
.panel > .micro + form.upload { margin-top: 12px; }

/* Board extras: the latest pass, and where to look next */
.latest { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-top: 18px; }
.latest .panel + .panel { margin-top: 0; }
.brief { display: flex; align-items: center; gap: 16px; margin-top: 14px; }
.brief h3 { margin: 0; }
.brief p { margin: 2px 0 0; color: var(--muted); font-size: 14px; }

/* The pilot page */
.stats { display: grid; grid-template-columns: repeat(3, 1fr); border-top: 1px solid var(--border); border-bottom: 1px solid var(--border); }
.stats > div { padding: 18px 22px; border-right: 1px solid var(--border); }
.stats > div:first-child { padding-left: 0; }
.stats > div:last-child { border-right: 0; }
.stats strong { display: block; font: 700 24px var(--mono); letter-spacing: -.04em; margin: 8px 0 3px; }
.stats small { color: var(--muted); font-size: 13px; }
.insight-grid { display: grid; grid-template-columns: 1.2fr 1fr; gap: 16px; }
.insight-grid .panel + .panel { margin-top: 0; }
.fault { display: flex; justify-content: space-between; gap: 16px; border-top: 1px solid var(--border); padding: 14px 0; }
.fault:first-of-type { margin-top: 12px; }
.fault strong { font-size: 15px; }
.fault small { display: block; color: var(--muted); font-size: 13px; margin-top: 3px; }
.fault b { font: 700 16px var(--mono); color: var(--accent); white-space: nowrap; }
.fault.none { color: var(--muted); font-size: 14px; }
.small-grades { display: flex; gap: 5px; flex-wrap: wrap; margin: 17px 0; }
.small-grades .cell { width: 40px; height: 42px; font-size: 11px; }
nav.airframes { display: flex; gap: 4px; flex-wrap: wrap; border-bottom: 1px solid var(--border); margin: 0 0 16px; }
nav.airframes a { padding: 8px 14px; color: var(--muted); text-decoration: none; border-bottom: 2px solid transparent;
  margin-bottom: -1px; font-weight: 700; }
nav.airframes a:hover { color: var(--text); }
nav.airframes a.current { color: var(--text); border-bottom-color: #d1bd78; }
nav.airframes .n { font-weight: 400; font-size: 12px; color: var(--muted); }
ul.themes { list-style: none; padding: 0; margin: 0; display: grid; gap: 8px; }
ul.themes li { display: flex; gap: 10px; align-items: baseline; }
.kind { font: 700 11px var(--mono); text-transform: uppercase; color: var(--muted); min-width: 64px; }
.bar { flex: none; width: 60px; height: 6px; background: var(--empty); overflow: hidden; align-self: center; }
.bar span { display: block; height: 100%; background: var(--muted); }
.recent { display: flex; flex-wrap: wrap; gap: 3px; margin-top: 8px; }

/* The pass page */
.sheet-head { background: var(--navy); color: #edf3eb; display: grid; grid-template-columns: 1fr auto; gap: 24px;
  align-items: center; padding: 24px 28px; border-top: 4px solid #bdb694; }
.sheet-head h2 { margin: 7px 0; font-size: 27px; letter-spacing: -.04em; }
.sheet-head p, .sheet-head .micro { margin: 0; color: #c5d5c8; }
.sheet-card { background: var(--card); border: 1px solid var(--border); border-top: 0; padding: 24px; }
.sheet-caption { display: flex; align-items: center; justify-content: space-between; gap: 15px; margin-bottom: 14px; flex-wrap: wrap; }
.sheet-caption h2 { font-size: 21px; margin: 4px 0; }
.detail-grid { display: grid; grid-template-columns: 1.2fr 1fr; gap: 16px; margin-top: 16px; }
.detail-grid .panel + .panel { margin-top: 0; }
.detail-list { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 18px; margin: 16px 0 0; }
.detail-list dt { font-size: 11px; letter-spacing: .08em; text-transform: uppercase; color: var(--muted); font-weight: 750; }
.detail-list dd { margin: 5px 0 0; font-size: 14px; font-weight: 600; overflow-wrap: anywhere; }
.detail-list div.wide { grid-column: 1 / -1; }
.quality { background: var(--surface2); }
.detail-grid.single { grid-template-columns: 1fr; }
.caption-right { display: flex; align-items: center; gap: 10px; margin-left: auto; }
.badges { display: flex; gap: 6px; }
.badge { color: #fff; font-size: 12px; font-weight: 700; padding: 3px 10px; border-radius: 999px; cursor: help; white-space: nowrap; }
.pop-host { position: relative; }
.pop-host .pop { display: none; position: absolute; right: 0; top: calc(100% + 8px); z-index: 20; width: max-content;
  max-width: min(720px, calc(100vw - 32px)); overflow-x: auto; background: var(--surface2); color: var(--text);
  border: 1px solid var(--border); padding: 14px 16px; box-shadow: 0 8px 24px rgba(0, 0, 0, .25); cursor: auto;
  font-weight: 400; white-space: normal; text-align: left; }
.pop-host:hover .pop, .pop-host:focus .pop, .pop-host:focus-within .pop { display: block; }
.pop-host .pop.text { width: max-content; max-width: min(320px, calc(100vw - 32px)); font-size: 13px; line-height: 1.45; }
.pop-host .pop.left { left: 0; right: auto; }
.stamp.pop-host { cursor: help; }
.stamp .pop { font: 13px/1.45 var(--font, system-ui), sans-serif; }
.pop-host .pop::before { content: ""; position: absolute; left: 0; right: 0; top: -10px; height: 10px; }
dl.facts { display: grid; grid-template-columns: max-content 1fr; gap: 4px 16px; margin: 0 0 16px; }
dl.facts dt { color: var(--muted); }
dl.facts dd { margin: 0; }
.card svg { display: block; width: 100%; height: auto; }
.accuracy { width: 100%; border-collapse: collapse; margin: 12px 0 0; font-size: 13px; }
.accuracy th { text-align: left; font: 700 11px var(--mono); letter-spacing: .06em; text-transform: uppercase; color: var(--muted); padding: 4px 10px 6px 0; }
.accuracy td { vertical-align: top; padding: 2px 10px 6px 0; }
.accuracy .split { border-left: 2px solid var(--border); padding-left: 10px; }
.accuracy .note { display: block; color: var(--muted); font-size: 12px; margin-top: 3px; }
.accuracy .yes { color: var(--good); font-weight: 700; } .accuracy .no { color: var(--danger); font-weight: 700; }
.level { display: inline-block; white-space: nowrap; padding: 1px 9px; color: #fff; font: 700 12px var(--mono); }
.level-Full { background: #397a51; } .level-High { background: #326587; } .level-Medium { background: #b08a2e; }
.level-Low { background: #a4473d; } .level-None { background: #596c6e; } .level-na { background: var(--empty); color: var(--muted); }
#tc-pop { position: fixed; z-index: 10; width: min(640px, 92vw); pointer-events: none; background: var(--card);
  border: 1px solid var(--border); padding: 6px; box-shadow: 0 8px 24px rgba(0, 0, 0, 0.25); }
#tc-pop svg { display: block; width: 100%; height: auto; }
#tc-pop .loading { color: var(--muted); padding: 24px; text-align: center; }
.zoomable { position: relative; cursor: crosshair; user-select: none; }
.zoomable.loading svg { opacity: 0.5; }
.zoom-sel { position: absolute; top: 0; bottom: 0; background: rgba(36, 91, 73, 0.14);
  border-left: 1px solid var(--accent); border-right: 1px solid var(--accent); pointer-events: none; }
.zoom-reset { font-size: 12px; font-weight: 600; margin-left: 8px; padding: 4px 10px; border: 1px solid var(--border);
  background: var(--paper-button); color: var(--paper-button-text); cursor: pointer; vertical-align: 2px; }

/* Forms: uploads, sign in, joining, settings */
.upload-grid { display: grid; grid-template-columns: 1.2fr .8fr; gap: 16px; align-items: start; }
.upload-grid .panel + .panel { margin-top: 0; }
form.upload { display: grid; gap: 14px; max-width: 640px; }
form.upload label { display: grid; gap: 6px; font-weight: 700; font-size: 14px; }
form.upload .hint { font-weight: 400; color: var(--muted); font-size: 13px; }
form.upload input:not([type=checkbox]):not([type=file]), form.upload select, form.upload textarea {
  min-height: 44px; padding: 9px 10px; border: 1px solid var(--border); background: var(--paper-button); color: var(--paper-button-text); }
form.upload input[type=file] { padding: 18px; border: 1px dashed var(--border); background: var(--surface2); color: var(--text); }
form.upload button[type=submit] { justify-self: start; min-height: 44px; padding: 10px 18px; cursor: pointer; font-weight: 750; }
.upload-panel { max-width: 760px; }
.upload-panel .actions { display: flex; gap: 9px; flex-wrap: wrap; }
progress { width: 100%; accent-color: var(--accent); }
table.results { border-collapse: collapse; width: 100%; }
table.results th, table.results td { text-align: left; padding: 8px; border-bottom: 1px solid var(--border); }
table.results th { color: var(--muted); font: 700 11px var(--mono); letter-spacing: .06em; text-transform: uppercase; }
.token-once { border: 1px solid var(--border); background: var(--surface2); padding: 8px 12px; margin: 8px 0; }
.token-once code { word-break: break-all; font-size: 13px; }
.notice { border: 1px solid #b08a2e; background: var(--surface2); padding: 10px 14px; margin: 0 0 16px; }
.notice .actions { margin-top: 6px; }
dialog.modal { max-width: min(640px, calc(100vw - 32px)); background: var(--card); color: var(--text);
  border: 1px solid var(--border); padding: 14px 18px; }
dialog.modal::backdrop { background: rgba(0, 0, 0, 0.5); }
dialog.modal h3 { margin: 8px 0 4px; }
button.link { background: none; border: 0; padding: 0; color: inherit; text-decoration: underline; cursor: pointer; }
form.inline { display: flex; gap: 6px; align-items: center; margin: 0; }
form.inline input { width: 9em; }
.admin pre { font-size: 12px; overflow-x: auto; border: 1px solid var(--border); padding: 8px; background: var(--surface2); }
.admin ul { margin: 4px 0 8px; padding-left: 20px; }
.admin .actions { display: flex; gap: 6px; flex-wrap: wrap; align-items: center; }
.admin .actions form { display: flex; gap: 6px; margin: 0; }
.admin button, .admin input, .admin textarea, .admin select { font-size: 13px; padding: 5px 9px;
  border: 1px solid var(--border); background: var(--paper-button); color: var(--paper-button-text); }
.admin button { cursor: pointer; } .admin button.danger, button.danger { color: var(--danger); }
.admin input.label { width: 8em; }
.admin textarea { width: 100%; min-height: 8em; font-family: var(--mono); }
table[data-sortable] th button { all: unset; cursor: pointer; font: inherit; }
table[data-sortable] th button:hover { text-decoration: underline; }
table[data-sortable] th button:focus-visible { outline: 2px solid var(--text); }
table[data-sortable] th[aria-sort=ascending] button::after { content: " ▲"; font-size: .8em; }
table[data-sortable] th[aria-sort=descending] button::after { content: " ▼"; font-size: .8em; }
.help-icon { display: inline-grid; place-items: center; width: 20px; height: 20px; border: 1px solid var(--border);
  border-radius: 50%; color: var(--muted); font: 700 12px/1 var(--font, system-ui), sans-serif; cursor: help; }
h3 .help-icon { margin-left: 8px; vertical-align: 2px; letter-spacing: 0; }
h2 .help-icon { margin-left: 10px; vertical-align: 3px; letter-spacing: 0; }
label .help-icon { margin-left: 8px; vertical-align: 1px; }
.pop-host .pop { letter-spacing: normal; text-transform: none; }
.help-icon:hover, .help-icon:focus { color: var(--text); border-color: var(--text); }
.admin .alias { display: inline-flex; gap: 4px; align-items: center; margin: 0 8px 2px 0; }
.admin .alias button { padding: 0 6px; font-size: 12px; }
.admin .tag { font: 700 11px var(--mono); padding: 1px 7px; background: var(--accent); color: var(--card); }
.settings .setting { display: grid; grid-template-columns: minmax(10em, 16em) minmax(0, 1fr); gap: 2px 12px;
  align-items: center; margin: 8px 0; }
.settings .setting input:not([type=checkbox]), .settings .setting select { max-width: 22em; width: 100%; }
.settings .setting input[type=checkbox] { justify-self: start; }
.settings .setting .hint { grid-column: 2; color: var(--muted); font-size: 12px; }
.settings .setting.unsaved > .name::after { content: " *"; color: var(--warn); font-weight: 700; }
.settings h2 { margin: 18px 0 4px; font-size: 18px; } .settings h2:first-child { margin-top: 0; }
.settings .warn { color: var(--warn); font-size: 13px; }
.settings fieldset { border: 1px solid var(--border); margin: 8px 0; padding: 6px 10px; }
.settings fieldset label { display: inline-flex; gap: 4px; align-items: center; margin: 2px 14px 2px 0; }
.settings fieldset .note { color: var(--muted); font-size: 12px; }
.settings table input { width: 7em; }
.settings table td.note { color: var(--muted); font-size: 12px; }
.settings .carriers input.unit { width: 14em; max-width: 38vw; }
.settings td.default { white-space: nowrap; }
.legend { display: flex; flex-wrap: wrap; gap: 12px; margin: 12px 0 0; color: var(--muted); font-size: 12px; }

@media (max-width: 1000px) {
  header.site .top { padding: 12px 24px; flex-wrap: wrap; gap: 8px; }
  nav.nav { order: 3; width: 100%; }
  main { padding: 27px 24px 60px; }
  footer.site { padding: 18px 24px 40px; }
  .latest, .insight-grid, .detail-grid, .upload-grid { grid-template-columns: 1fr; }
  .stats > div { padding: 14px 12px; }
}
@media (max-width: 600px) {
  main { padding: 24px 16px 50px; }
  form.filters.compact { flex-wrap: wrap; }
  header.site .top { padding: 12px 16px; }
  footer.site { padding: 18px 16px 32px; }
  .brand small { display: none; }
  .page-header { align-items: start; flex-direction: column; }
  .board-frame { padding: 6px; }
  .board-heading { align-items: start; flex-direction: column; padding: 16px; gap: 9px; }
  .board-strip, .board-note { padding: 9px 16px; flex-direction: column; gap: 4px; }
  table.board th.pilot { width: 130px; padding-left: 10px; }
  table.board td.pilot { padding: 10px; position: sticky; left: 0; z-index: 1; background: #eeeade; }
  table.board th.pilot { position: sticky; left: 0; z-index: 1; }
  table.board td.pilot a { font-size: 15px; }
  .panel, .sheet-card { padding: 18px; }
  .sheet-head { padding: 20px; }
  .detail-list { grid-template-columns: 1fr; }
  .settings .setting { grid-template-columns: minmax(0, 1fr); }
.settings .setting .hint { grid-column: 1; }
  footer.site > div { display: block; }
  footer.site span { display: block; margin-top: 6px; }
  .board-hint { display: block; }
}
"""

# Each grade's colour, overridable by its variable (cards.theme.GRADE_VARS) in the admins' custom CSS: the light
# shade in both modes (the board is paper on the wall), white text but on (OK)'s yellow.
GRADE_CSS = "\n".join(
    f'.g-{i} {{ background: var({GRADE_VARS[g]}, {light});{" color: #172c27;" if g == "(OK)" else ""} }}'
    for i, (g, (light, _)) in enumerate(GRADE_COLORS.items()))
GRADE_CLASS = {g: f"g-{i}" for i, g in enumerate(GRADE_COLORS)}

# Who the request is signed in as (set per request by the app), for the bar at the top of every page.
CURRENT_PILOT: ContextVar[str | None] = ContextVar("current_pilot", default=None)
CURRENT_ADMIN: ContextVar[bool] = ContextVar("current_admin", default=False)
# For an admin: how many pilots are waiting for approval (shown on the Admin link).
CURRENT_PENDING: ContextVar[int] = ContextVar("current_pending", default=0)
# The signed-in pilot is waiting for an admin's approval (no Live link for them yet).
CURRENT_WAITING: ContextVar[bool] = ContextVar("current_waiting", default=False)
# The admins' custom CSS (set per request by the app), added after the built-in styles on every page.
CURRENT_CSS: ContextVar[str] = ContextVar("current_css", default="")
# The admins' icon's version (set per request by the app), or None for the built-in "GB" mark.
CURRENT_ICON: ContextVar[str | None] = ContextVar("current_icon", default=None)


# The header's Light/Dark toggle: the viewer's choice is kept in this browser (`burble-theme`); until they make one,
# the page follows their system. THEME_HEAD runs before the page draws, so it never flashes the other mode.
THEME_HEAD = ("<script>try{const t=localStorage.getItem('burble-theme');"
              "if(t==='light'||t==='dark')document.documentElement.dataset.theme=t}catch(e){}</script>")
THEME_SCRIPT = """
(() => {
  const button = document.getElementById('theme-toggle');
  if (!button) return;
  const root = document.documentElement;
  const system = window.matchMedia('(prefers-color-scheme: dark)');
  const dark = () => (root.dataset.theme || (system.matches ? 'dark' : 'light')) === 'dark';
  const label = () => {
    button.textContent = dark() ? '\u263c Light' : '\u25d0 Dark';
    button.setAttribute('aria-label', dark() ? 'Switch to the light theme' : 'Switch to the dark theme');
  };
  button.addEventListener('click', () => {
    const theme = dark() ? 'light' : 'dark';
    root.dataset.theme = theme;
    try { localStorage.setItem('burble-theme', theme); } catch (e) {}
    label();
  });
  system.addEventListener('change', label);
  label();
})();
"""


def _nav(section: str | None = None) -> str:
    """The header: the brand, the sections (`section` is the current one; "me" and "settings" are the signed-in
    pilot's own page and settings, on the right), the theme toggle, and who's signed in."""
    me = CURRENT_PILOT.get()
    links = [("board", "/", "Board"), ("upload", "/upload", "Upload")]  # Upload in the same place for everyone
    if me and not CURRENT_WAITING.get():  # Live isn't open until an admin approves them
        links.append(("live", "/live", "Live"))
    if me and CURRENT_ADMIN.get():
        n = CURRENT_PENDING.get()
        links.append(("admin", "/admin", f"Admin{f' ({n})' if n else ''}"))
    nav = "".join(f'<a href="{href}"{" aria-current=page" if key == section else ""}>{escape(label)}</a>'
                  for key, href, label in links)
    toggle = '<button type="button" class="theme-toggle" id="theme-toggle">\u25d0 Dark</button>'
    if me:
        quoted = quote(me, safe="")
        here = {key: " aria-current=page" if key == section else "" for key in ("me", "settings")}
        right = (f'<a class="simple me" href="/pilots/{quoted}" title="Signed in as {escape(me)}"{here["me"]}>{escape(me)}</a>'
                 f'<a class="simple" href="/pilots/{quoted}/settings"{here["settings"]}>Settings</a>'
                 '<form method="post" action="/signout"><button type="submit" class="simple">Sign out</button></form>')
    else:
        right = '<a class="simple" href="/signin">Sign in</a><a class="button lime" href="/join">Join</a>'
    icon = CURRENT_ICON.get()
    mark = (f'<span class="brandmark custom"><img src="/icon?v={escape(icon)}" alt=""></span>' if icon
            else '<span class="brandmark">GB</span>')
    return ('<header class="site"><div class="top"><a class="brand" href="/">' + mark +
            '<span><strong>GREENIE BOARD</strong><small>CARRIER LANDING RECORDS</small></span></a>'
            f'<nav class="nav" aria-label="Sections">{nav}</nav><div class="header-right">{toggle}{right}</div></div></header>')


def _pilot_section(name: str, own: str = "me", other: str | None = "board") -> str | None:
    """Which header tab a pilot's page sits under: `own` (on the right) when it's the signed-in pilot's, else `other`."""
    return own if name == CURRENT_PILOT.get() else other


def _header(title: str, sub: str = "", actions: str = "", micro: str = "") -> str:
    """A page's heading: the title (HTML; none where the header's tab says where you are), a small label under it
    (`micro`), a line under that (HTML), and buttons on the right (`actions`, HTML)."""
    under = f'<div class="micro quiet under">{micro}</div>' if micro else ""
    return (f'<div class="page-header"><div>{f"<h1>{title}</h1>" if title else ""}{under}{f"<p>{sub}</p>" if sub else ""}</div>'
            + (f'<div class="actions">{actions}</div>' if actions else "") + "</div>")


def _page(title: str, body: str, section: str | None = None) -> str:
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">'
            f"<title>{escape(title)}</title>"
            + (f'<link rel="icon" href="/icon?v={escape(icon)}">' if (icon := CURRENT_ICON.get()) else "")
            + f"{THEME_HEAD}<style>{STYLE}\n{GRADE_CSS}</style>"
            + (f"<style>{safe_css(css)}</style>" if (css := CURRENT_CSS.get()) else "") + "</head>"
            f"<body>{_nav(section)}<main>{body}</main>"
            '<footer class="site"><div><span>Powered by <a href="https://github.com/wrycu/dcs-burble">Burble</a></span>'
            "<span>Times are UTC</span></div></footer>"
            f"<script>{THEME_SCRIPT}{ONCE_SCRIPT}</script></body></html>")


# A step's message (`?done=`, `?error=`, after a redirect) is shown once: taken out of the address, so reloading or
# coming back to the page doesn't show it again.
ONCE_SCRIPT = """
(() => {
  const url = new URL(location.href);
  if (!url.searchParams.has('done') && !url.searchParams.has('error')) return;
  url.searchParams.delete('done');
  url.searchParams.delete('error');
  history.replaceState(null, '', url.pathname + (url.search || '') + url.hash);
})();
"""


def _when(dt: datetime | None) -> str:
    return dt.strftime("%Y-%m-%d %H:%M UTC") if dt else "unknown time"


def board_page(passes: list[Pass], pilots: list[str], days: int, chosen: list[str], columns: int,
               empty: bool = False, servers: str = "all", aircraft: str | None = None,
               airframes: list[str] | None = None) -> str:
    """A framed board per airframe (geometry.airframe: the F-14A and B are one), each pilot's landings in it.
    `chosen`: the pilots the board is filtered to (none: everyone).
    `empty`: also list pilots with no passes in the filter (e.g. joined, not flown here yet). `servers`: "ours"
    leaves out landings flown on other servers; `aircraft`: one airframe only (the passes are already
    filtered; these set the form). `airframes`: the choices for the Aircraft filter."""
    sections: dict[str, dict[str, list[Pass]]] = defaultdict(lambda: defaultdict(list))
    for p in sorted(passes, key=lambda p: (p.occurred_at or p.created_at)):
        sections[airframe(p.aircraft_type)][p.pilot.name].append(p)

    def option(value: str, label: str, selected: str | None) -> str:
        sel = " selected" if value == (selected or "") else ""
        return f'<option value="{escape(value)}"{sel}>{escape(label)}</option>'

    periods = ((7, "7 days"), (30, "30 days"), (90, "90 days"), (365, "1 year"), (0, "All time"))
    filters = (
        '<form class="filters" method="get">'
        '<label>Period <select name="days">'
        + "".join(option(str(d), label, str(days)) for d, label in periods)
        + "</select></label>" + _pilot_picker(pilots, chosen)
        + '<label>Aircraft <select name="aircraft">' + option("", "All aircraft", aircraft)
        + "".join(option(n, n, aircraft) for n in (airframes or []))
        + '</select></label><label>Servers <select name="servers">'
        + option("ours", "This hub's servers", servers) + option("all", "All servers", servers)
        + '</select></label><label class="check"><input type="checkbox" name="empty" value="1"'
        + (" checked" if empty else "") + "> Show pilots with no passes</label>"
        + '<button type="submit">Apply</button></form>'
    )

    def no_passes_row(name: str) -> str:
        return (f'<tr><td class="pilot"><a href="/pilots/{quote(name, safe="")}">{escape(name)}</a></td>'
                '<td class="num">0</td><td class="num">–</td><td class="num">–</td>'
                + '<td><span class="tile empty"></span></td>' * columns + "</tr>")

    def pilot_row(name: str, items: list[Pass]) -> str:
        graded = [p for p in items if p.grade]
        traps = sum(p.outcome == "trap" for p in items)
        avg = sum(p.grade.points for p in graded) / len(graded) if graded else 0.0
        modex = items[-1].pilot.modex
        shown = items[-columns:]
        cells = []
        for p in shown:
            g = p.grade.grade if p.grade else "?"
            night = " · night" if p.night else ""
            # The date and time first (a line each: date and time, grade, mission).
            title = "\n".join(x for x in (_when(p.occurred_at),
                                          f"{grade_name(g)}: {p.grade.text if p.grade else ''}{night}", p.mission) if x)
            cells.append(f'<td><a class="tile {GRADE_CLASS.get(g, "")}{" night" if p.night else ""}" href="/passes/{p.id}" '
                         f'title="{escape(title)}" aria-label="{escape(grade_name(g))}, {escape(_when(p.occurred_at))}">'
                         "</a></td>")  # the colour says the grade (the key is below); the letter is in the tooltip
        cells += ['<td><span class="tile empty"></span></td>'] * (columns - len(shown))
        side = f'<span class="side">#{escape(modex)}</span>' if modex else ""
        return (f'<tr><td class="pilot"><a href="/pilots/{quote(name, safe="")}" title="Themes across recent passes">'
                f'{escape(name)}</a>{side}</td><td class="num">{len(items)}</td>'
                f'<td class="num">{avg:.2f}</td><td class="num">{100 * traps / len(items):.0f}%</td>'
                + "".join(cells) + "</tr>")

    def frame(title: str, rows: list[str], items: list[Pass]) -> str:
        heads = "".join(f'<th scope="col">{n:02d}</th>' for n in range(1, columns + 1))
        traps = sum(p.outcome == "trap" for p in items)
        note = (f'<strong>{len(items)} pass{"es" if len(items) != 1 else ""}</strong> &nbsp;/&nbsp; '
                f'{100 * traps / len(items):.0f}% traps' if items else "<strong>No passes</strong>")
        return ('<div class="board-frame"><div class="board-paper"><div class="board-heading"><div>'
                f'<h2 class="airframe">{escape(title)}</h2></div></div>'
                f'<div class="board-strip"><span>{len(rows):02d} pilot{"s" if len(rows) != 1 else ""} shown</span></div>'
                f'<div class="board-scroll" tabindex="0" role="region" aria-label="{escape(title)} board, scrolls sideways">'
                f'<table class="board" style="min-width:{170 + 3 * 68 + columns * 45}px"><thead><tr>'
                '<th scope="col" class="pilot">Pilot</th><th scope="col" class="num">Passes</th>'
                f'<th scope="col" class="num">Avg</th><th scope="col" class="num">Traps</th>{heads}</tr></thead>'
                f'<tbody>{"".join(rows)}</tbody></table></div>'
                f'<div class="board-note"><span>{note}</span><span>Choose a grade to open its debrief.</span></div>'
                '</div></div><p class="board-hint">Swipe across the board to see the passes →</p>')

    # The busiest airframe first.
    order = sorted(sections, key=lambda a: (-sum(len(v) for v in sections[a].values()), a.lower()))
    parts = [frame(a, [pilot_row(n, sections[a][n]) for n in sorted(sections[a], key=str.lower)],
                   [p for v in sections[a].values() for p in v]) for a in order]
    if empty:
        flown = {n for by_pilot in sections.values() for n in by_pilot}
        idle = sorted((n for n in pilots if n not in flown and (not chosen or n in chosen)), key=str.lower)
        if idle:
            parts.append(frame("No passes yet", [no_passes_row(n) for n in idle], []))
    content = "".join(parts) if parts else ('<div class="board-frame"><div class="board-paper">'
                                            '<p class="empty-state">No passes yet for this filter.</p></div></div>')

    key = '<div class="key">' + "".join(
        f'<span><span class="swatch {GRADE_CLASS[g]}"></span><strong>{escape(grade_short(g))}</strong> {grade_name(g)}</span>'
        for g in GRADE_COLORS
    ) + '<span><span class="night-dot"></span>Night pass</span></div>'
    body = (content + key
            + '<div class="section-header"><div class="micro quiet scope">Board scope</div></div>'
            + filters + _latest(passes, chosen[0] if len(chosen) == 1 else None))
    return _page("Greenie Board", body, "board")


def _pilot_picker(pilots: list[str], chosen: list[str]) -> str:
    """The board's Pilots filter: a box naming who's chosen, opening onto a search and a checkbox per pilot (sent as
    repeated `pilot=`). Without JavaScript it's the checkboxes alone."""
    chosen = [n for n in pilots if n in chosen] + [n for n in chosen if n not in pilots]
    label = ("All pilots" if not chosen else chosen[0] if len(chosen) == 1
             else ", ".join(chosen) if len(chosen) <= 3 else f"{len(chosen)} pilots")
    names = "".join(
        f'<label data-name="{escape(n.lower())}"><input type="checkbox" name="pilot" value="{escape(n)}"'
        f'{" checked" if n in chosen else ""}> {escape(n)}</label>' for n in chosen + [n for n in pilots if n not in chosen])
    return ('<div class="field">Pilots<details class="pick" id="pilot-pick">'
            f'<summary aria-label="Pilots: {escape(label)}">{escape(label)}</summary><div class="drop">'
            '<input type="search" placeholder="Search pilots" aria-label="Search pilots" autocomplete="off" hidden>'
            f'<div class="names">{names}</div><p class="none" hidden>No pilot by that name.</p>'
            '<button type="button" class="clear" hidden>Clear (all pilots)</button></div></details></div>'
            f"<script>{PICKER_SCRIPT}</script>")


PICKER_SCRIPT = """
(() => {
  const pick = document.getElementById('pilot-pick');
  if (!pick) return;
  const search = pick.querySelector('input[type=search]'), none = pick.querySelector('.none');
  const clear = pick.querySelector('.clear'), summary = pick.querySelector('summary');
  const boxes = [...pick.querySelectorAll('.names input')];
  search.hidden = false;
  const update = () => {
    const names = boxes.filter(b => b.checked).map(b => b.value);
    const text = !names.length ? 'All pilots' : names.length <= 3 ? names.join(', ') : names.length + ' pilots';
    summary.textContent = text;
    summary.setAttribute('aria-label', 'Pilots: ' + text);
    clear.hidden = !names.length;
  };
  search.addEventListener('input', () => {
    const q = search.value.trim().toLowerCase();
    let shown = 0;
    boxes.forEach(b => { const hit = b.parentElement.dataset.name.includes(q); b.parentElement.hidden = !hit; shown += hit; });
    none.hidden = shown > 0;
  });
  search.addEventListener('keydown', e => {
    if (e.key !== 'Enter') return;
    e.preventDefault();  // Enter ticks the only match rather than submitting the form
    const shown = boxes.filter(b => !b.parentElement.hidden);
    if (shown.length === 1) { shown[0].checked = !shown[0].checked; update(); }
  });
  boxes.forEach(b => b.addEventListener('change', update));
  clear.addEventListener('click', () => { boxes.forEach(b => b.checked = false); update(); });
  pick.addEventListener('toggle', () => { if (pick.open) search.focus(); });
  document.addEventListener('click', e => { if (pick.open && !pick.contains(e.target)) pick.open = false; });
  document.addEventListener('keydown', e => { if (e.key === 'Escape' && pick.open) { pick.open = false; summary.focus(); } });
  update();
})();
"""


def _latest(passes: list[Pass], pilot: str | None) -> str:
    """Under the board: the newest pass in it, and the pilot page to look at next (that pass's pilot, or the one
    the board is filtered to)."""
    if not passes:
        return ""
    p = max(passes, key=lambda p: p.occurred_at or p.created_at)
    g = p.grade.grade if p.grade else "?"
    name = pilot or p.pilot.name
    quoted = quote(name, safe="")
    return ('<div class="latest"><div class="panel">'
            f'<div class="micro quiet">Latest pass &nbsp;/&nbsp; {escape(_when(p.occurred_at))}</div>'
            f'<div class="brief"><a class="stamp {GRADE_CLASS.get(g, "")}" href="/passes/{p.id}" '
            f'aria-label="{escape(grade_name(g))}">{escape(grade_short(g))}</a><div>'
            f"<h3>{escape(p.pilot.name)} · {escape(grade_name(g))}</h3>"
            f"<p>{escape(_outcome(p))} · {escape(airframe(p.aircraft_type))}</p></div></div>"
            f'<p style="margin-top:15px"><a class="text-link" href="/passes/{p.id}">Open the pass and trap card →</a></p>'
            '</div><div class="panel"><div class="micro quiet">Pilot review</div><h3>Where to look next</h3>'
            "<p>The pilot page brings recent grades, the faults that keep showing up, and their traps overlaid "
            f'together.</p><a class="text-link" href="/pilots/{quoted}">Open {escape(name)}’s record →</a></div></div>')


def _outcome(p: Pass) -> str:
    """The outcome in words, with the wire for a trap: "Trap · wire 3 (DCS)"."""
    words = {"trap": "Trap", "bolter": "Bolter", "waveoff": "Wave-off", "crash": "Crash",
             "incomplete": "Incomplete"}.get(p.outcome, p.outcome.capitalize())
    if p.outcome != "trap":
        return words
    if p.wire is not None:
        return f"{words} · {p.wire} wire"
    estimate = ((p.grade.detail or {}) if p.grade else {}).get("wire_estimate")
    return f"{words} · {estimate} wire (estimated)" if estimate is not None else f"{words} · wire not known"


def card_title(p: Pass) -> str:
    """The trap card's title line: the mission, the pilot's side number, and the livery, when known."""
    modex = p.pilot.modex if p.pilot else None
    return " · ".join(x for x in (p.mission, f"#{modex}" if modex else None, p.livery) if x)


def _wire(p: Pass) -> str:
    """DCS's own wire when known; otherwise the estimate from the stop point, marked as one."""
    if p.wire is not None:
        return f"#{p.wire} (DCS)"
    estimate = ((p.grade.detail or {}) if p.grade else {}).get("wire_estimate")
    return f"#{estimate} (estimated from where the jet stopped)" if estimate is not None else "–"


def _report(r: Pass, used: bool) -> str:
    info = (r.slice.sidecar or {}).get("pass") or {}
    rate = info.get("sample_rate_hz")
    detail = ", ".join(x for x in (f"{rate:g} Hz" if rate else "", "AOA" if info.get("aoa_recorded") else "",
                                   "own track only" if r.is_track else "") if x)
    text = f"{r.source.name} ({r.source.kind}{', ' + detail if detail else ''})"
    return (f'<a href="/passes/{r.id}/acmi">{escape(text)}</a>'
            + (" <strong>track used</strong>" if used else ""))


ACCURACY_COLORS = {"Full": "#1a7f37", "High": "#0969da", "Medium": "#bf8700", "Low": "#cf4d1a", "None": "#8c959f"}
ELSEWHERE_TIP = "Flown on a server that isn't tracked by this hub."


def _accuracy_badges(a: Accuracy | None) -> str:
    """The pass page's badges over the trap card: the overall accuracy, whose tooltip is the whole Source and
    confidence table, and "another server" (flown elsewhere), with a tooltip saying what that means."""
    if a is None:
        return ""
    level = a.overall.level
    out = (f'<span class="badge pop-host" tabindex="0" style="background:{ACCURACY_COLORS.get(level, "#8c959f")}">'
           f'{escape(level)} accuracy<span class="pop" role="tooltip"><strong>Source and confidence</strong>'
           f"{_accuracy(a)}</span></span>")
    if a.flown.level == "elsewhere":
        out = ('<span class="badge pop-host" tabindex="0" style="background:#6e40c9">another server'
               f'<span class="pop text" role="tooltip">{escape(ELSEWHERE_TIP)}</span></span>') + out
    return f'<span class="badges">{out}</span>'


def _accuracy(a: Accuracy) -> str:
    """The landing's row of the accuracy table (docs/accuracy-scores.png): which parts reported it, and the scores."""
    def level(s, noted: bool = True) -> str:
        cls = "na" if s.level == "n/a" else s.level
        note = f'<span class="note">{escape(s.note)}</span>' if s.note and noted else ""
        return f'<span class="level level-{cls}">{escape(s.level)}</span>{note}'

    heads = [p.name for p in a.parts] + ["Overall", "Approach", "Wire", "Comms"]
    cells = [f'<span class="{"yes" if p.present else "no"}">{"✓" if p.present else "✗"}</span>'
             + (f'<span class="note">{escape(p.note)}</span>' if p.note else "") for p in a.parts]
    cells += [level(s, name != "comms") for name, s in a.scores().items()]  # Comms: just the level
    split = len(a.parts)
    th = "".join(f'<th{" class=split" if i == split else ""}>{escape(h)}</th>' for i, h in enumerate(heads))
    td = "".join(f'<td{" class=split" if i == split else ""}>{c}</td>' for i, c in enumerate(cells))
    return f'<div class="scroll-x"><table class="accuracy"><tr>{th}</tr><tr>{td}</tr></table></div>'


def weather_text(weather: dict | None) -> str | None:
    """The mission's weather settings (the server hook's `weather` event) in a line: clouds, visibility, fog,
    temperature and pressure."""
    if not weather:
        return None
    parts = []
    if weather.get("dynamic"):
        parts.append("dynamic weather")
    clouds = weather.get("clouds") or {}
    if clouds.get("preset") or clouds.get("density"):
        base = clouds.get("base_m")
        parts.append("clouds" + (f" from {base / 0.3048:,.0f} ft" if base is not None else ""))
    else:
        parts.append("clear skies")
    precipitation = {1: "rain", 2: "thunderstorms", 3: "snow", 4: "snowstorms"}.get(clouds.get("precipitation") or 0)
    if precipitation:
        parts.append(precipitation)
    if (mist := fog(weather)) is not None:
        parts.append(f"fog (visibility {mist['visibility_m'] / 1852:.1f} nm)")
    if (vis := weather.get("visibility_m")) is not None:
        parts.append(f"visibility {vis / 1852:.0f} nm")
    if weather.get("dust_m"):
        parts.append(f"dust (visibility {weather['dust_m'] / 1852:.1f} nm)")
    if (temp := weather.get("temperature")) is not None:
        parts.append(f"{temp:g} °C")
    if (qnh := weather.get("qnh_mmhg")) is not None:
        parts.append(f"QNH {qnh / 25.4:.2f} inHg")
    return ", ".join(parts)


def _grade_stamp(g) -> str:
    """The pass sheet's grade square: colour only, with what it means and its points in a themed popup."""
    grade = g.grade if g else "?"
    tip = (f"<strong>{escape(grade_short(grade))}: {escape(grade_name(grade))}</strong><br>{g.points:g} points"
           if g else "<strong>Not graded</strong>")
    label = f"{grade_name(grade)}, {g.points:g} points" if g else "Not graded"
    return (f'<div class="stamp pop-host {GRADE_CLASS.get(grade, "")}" tabindex="0" role="img" aria-label="{escape(label)}">'
            f'<span class="pop text" aria-hidden="true">{tip}</span></div>')


def pass_page(p: Pass, card_svg: str | None, error: str | None = None, reports: list[Pass] | None = None,
              track_source: str | None = None, accuracy: Accuracy | None = None, wind: str | None = None,
              weather: dict | None = None, radio: list[dict] | None = None) -> str:
    g = p.grade
    facts = [("Wind", escape(wind.removeprefix("Wind ")))] if wind else []  # first, at the top
    facts += [
        ("Carrier", escape(f"{p.carrier_unit or ''} ({p.carrier_type})")),
        ("Mission", escape(p.mission or "–")),
        ("Side number", escape(p.pilot.modex or "–")),
        ("Livery", escape(p.livery or "–")),
        ("Wire", escape(_wire(p))),
    ]
    wide = []
    if (settings := weather_text(weather)) is not None:
        wide.append(("Weather", escape(settings)))
    if accuracy is not None:
        facts.append(("Flown on", escape(accuracy.flown_label + (f" ({accuracy.flown.note})" if accuracy.flown.note else ""))))
    if radio:
        wide.append(("Radio", _radio(p, radio)))
    reports = reports or [p]
    used = track_source or p.source.name
    listed = "<br>".join(_report(r, len(reports) > 1 and r.source.name == used) for r in reports)
    facts.append(("Reports" if len(reports) > 1 else "Source", listed))
    dl = ("".join(f"<div><dt>{k}</dt><dd>{v}</dd></div>" for k, v in facts)
          + "".join(f'<div class="wide"><dt>{k}</dt><dd>{v}</dd></div>' for k, v in wide))
    name = escape(p.pilot.name)
    quoted = quote(p.pilot.name, safe="")
    grade = g.grade if g else "?"
    head = ('<div class="sheet-head"><div>'
            f'<h2>{name} &nbsp;/&nbsp; Pass {p.id}{f" &nbsp;/&nbsp; grading v{g.version}" if g else ""}</h2>'
            f"<p>{escape(_outcome(p))}{' · night' if p.night else ''}</p>"
            f'</div>{_grade_stamp(g)}</div>')
    if card_svg:
        card = ('<div class="sheet-card"><div class="sheet-caption"><h2>Trap Sheet</h2>'
                '<div class="caption-right"><button id="card-zoom-reset" class="zoom-reset" hidden>Reset zoom</button>'
                f"{_accuracy_badges(accuracy)}</div></div>"
                f'<div class="card zoomable" id="trap-card" data-src="/passes/{p.id}/card.svg?zoom=1" '
                f'data-reset="card-zoom-reset">{card_svg}</div></div><script>{ZOOM_SCRIPT}</script>')
    else:
        card = f'<div class="sheet-card"><p class="empty-state">{escape(error or "")}</p></div>'
    # With a card, the accuracy table is the tooltip of its badge; without one, a panel beside the details.
    quality = ('<div class="panel quality"><h3>Source and confidence</h3>'
               f'{_accuracy(accuracy)}</div>' if accuracy and not card_svg else "")
    actions = (f'<a class="button paper" href="/pilots/{quoted}">← {name}</a>'
               f'<a class="button paper" href="/passes/{p.id}/acmi">Download ACMI</a>'
               + (f'<a class="button dark" href="/passes/{p.id}/replay">▶ &nbsp;Replay in 3D</a>' if card_svg else ""))
    sub = " · ".join(escape(x) for x in (_when(p.occurred_at), p.aircraft_type,
                                         f"#{p.pilot.modex}" if p.pilot.modex else "") if x)
    body = (_header("Debrief", sub, actions=actions)
            + head + card
            + f'<div class="detail-grid{"" if quality else " single"}"><div class="panel"><h3>Flight details</h3>'
            f'<dl class="detail-list">{dl}</dl></div>{quality}</div>')
    return _page(f"{p.pilot.name} {g.grade if g else ''}", body)  # no tab: a pass isn't the board


def _radio(p: Pass, radio: list[dict]) -> str:
    """The pass's radio recording (PLAN #52): what's in it, and for its pilot or an admin, a way to delete it."""
    seconds = sum(t["seconds"] for t in radio)
    pilot = sum(t["by"] == "pilot" for t in radio)
    text = escape(f"recorded on the carrier frequency: {len(radio)} transmission{'s' if len(radio) != 1 else ''} "
                  f"({seconds:.0f} s, {pilot} from the pilot), played in the replay")
    if CURRENT_ADMIN.get() or CURRENT_PILOT.get() == p.pilot.name:
        ask = _js_text("Delete this pass's radio recording? It can't be undone.")
        text += (f' <form method="post" action="/passes/{p.id}/radio/delete" style="display:inline" '
                 f'onsubmit="return confirm(\'{ask}\')"><button type="submit" class="danger">Delete recording</button>'
                 f'</form>')
    return text


# three.js for the replay, served by the hub (static/; MIT licence alongside it).
THREE_DIR = "/static/three-0.169.0"

REPLAY_STYLE = """
[hidden] { display: none !important; }
.replay { position: relative; background: #000; border-radius: 12px; overflow: hidden; }
.replay canvas { display: block; width: 100%; height: min(70vh, 640px); min-height: 320px; touch-action: none; }
.replay .loading { position: absolute; inset: 0; display: grid; place-items: center; color: #ccc; }
.replay.ready .loading { display: none; }
.replay .hud { position: absolute; top: 10px; left: 10px; display: grid; grid-template-columns: max-content max-content;
  gap: 0 10px; padding: 6px 10px; border-radius: 8px; background: rgba(0, 0, 0, 0.55); color: #f0f3f6;
  font: 12px/1.6 ui-monospace, SFMono-Regular, Menlo, monospace; pointer-events: none; }
.replay .hud dt { color: #aab4be; }
.replay .hud dd { margin: 0; }
.replay .ballbox { position: absolute; top: 10px; right: 10px; width: 22px; height: 140px; border-radius: 6px;
  background: rgba(0, 0, 0, 0.55); pointer-events: none; }
.replay .ballbox::before, .replay .ballbox::after { content: ""; position: absolute; top: calc(50% - 2px);
  width: 14px; height: 4px; background: #3fdc5a; border-radius: 2px; }
.replay .ballbox::before { right: 100%; margin-right: 2px; }
.replay .ballbox::after { left: 100%; margin-left: 2px; }
.replay .ball { position: absolute; left: 3px; width: 16px; height: 16px; margin-top: -8px; border-radius: 50%;
  background: #ffa020; box-shadow: 0 0 8px #ffa020; }
.replay .ball.low { background: #ff3030; box-shadow: 0 0 8px #ff3030; }
.replay .call { position: absolute; bottom: 14px; left: 50%; transform: translateX(-50%); max-width: calc(100% - 32px);
  padding: 4px 12px; border-radius: 6px; background: rgba(0, 0, 0, 0.65); color: #fff; font-size: 16px;
  text-align: center; pointer-events: none; }
/* The PLAT view, as the carrier's deck camera monitor shows it (and DCS's Supercarrier draws it): a grey 4:3 picture,
   a black crosshair with a gap around the jet, and the camera's data in big typewriter digits. */
.replay .platframe { display: none; position: absolute; top: 0; bottom: 0; left: 50%; transform: translateX(-50%);
  aspect-ratio: 1274 / 928; max-width: 100%; container-type: size; pointer-events: none;
  box-shadow: 0 0 0 100vmax #0b0d0c;
  background: radial-gradient(ellipse at center, transparent 55%, rgba(0, 0, 0, 0.35) 100%),
    repeating-linear-gradient(0deg, rgba(0, 0, 0, 0.04) 0 1px, transparent 1px 3px); }
.replay.plat .platframe { display: block; }
.replay.plat canvas { filter: grayscale(1) contrast(0.8) brightness(1.12) blur(0.4px); }
.replay.plat .hud, .replay.plat .ballbox { display: none; }
.replay .platframe span { position: absolute; color: #141414; font: 7.6cqh/1 "Courier New", Courier, monospace;
  white-space: pre; text-shadow: 0 0 1px rgba(0, 0, 0, 0.4); }
.replay .platframe .line { position: absolute; background: #161616; }
.replay-bar { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin: 10px 0 4px; }
.replay-bar button, .replay-bar select { font: inherit; padding: 4px 10px; border-radius: 6px; cursor: pointer;
  border: 1px solid var(--border); background: var(--card); color: var(--text); }
.replay-bar button[aria-pressed="true"] { border-color: var(--text); font-weight: 600; }
.replay-bar input[type=range] { flex: 1 1 200px; }
.replay-bar .time { font-variant-numeric: tabular-nums; color: var(--muted); min-width: 9em; }
.replay-bar .group { display: flex; flex-wrap: wrap; gap: 4px; }
.replay-bar label { display: flex; gap: 4px; align-items: center; color: var(--muted); }
.lineup { margin-top: 10px; }
"""


def replay_page(p: Pass, lineup_svg: str | None = None) -> str:
    """The 3D replay of a landing (static/replay.js draws it from /passes/{id}/replay.json), with the trap card's
    lineup plot (`lineup_svg`, cards.render_lineup) below it, which follows the jet."""
    g = p.grade
    sub = (f"{escape(p.pilot.name)} · {escape(grade_name(g.grade) if g else '?')}"
           f'{" · " + escape(g.text) if g and g.text else ""} · {escape(_when(p.occurred_at))} · '
           f'{escape(p.aircraft_type)} · {escape(p.carrier_unit or p.carrier_type)}'
           f'{" · wire " + escape(_wire(p)) if p.wire else ""}')
    body = (_header("3D replay.", sub,
                    actions=f'<a class="button paper" href="/passes/{p.id}">← Debrief</a>')
            + f'<div class="replay" id="replay" data-src="/passes/{p.id}/replay.json">'
            f'<canvas aria-label="3D replay of the pass"></canvas>'
            + _scene_overlays("Loading the replay…") + '</div>'
            f'<div class="replay-bar"><button type="button" id="replay-play">Pause</button>'
            f'<input type="range" id="replay-scrub" min="0" value="0" aria-label="Time">'
            f'<span class="time" id="replay-time"></span>'
            f'<select id="replay-speed" aria-label="Speed">'
            + "".join(f'<option value="{v}"{" selected" if v == "1" else ""}>{v}×</option>'
                      for v in ("0.25", "0.5", "1", "2", "4"))
            + '</select><button type="button" id="replay-sound" aria-pressed="true" hidden>Sound on</button>'
            '<button type="button" id="replay-restart">From the start</button>'
            f'<button type="button" id="replay-groove">From the groove</button></div>'
            f'<div class="replay-bar"><span class="group" id="replay-views"></span>'
            f'<label><input type="checkbox" data-guide="glidepath" checked> Glide path</label>'
            f'<label><input type="checkbox" data-guide="path" checked> Path flown</label></div>'
            + (f'<div class="panel card lineup" id="replay-lineup">{lineup_svg}</div>' if lineup_svg else "") +
            f'<p class="sub" id="replay-hint"></p>'
            f'<p class="sub">Space plays and pauses, the arrow keys step a second (with Shift, five), 1–6 pick the '
            f"camera, and the mouse wheel zooms. The jet and ship are simple stand-in models; positions are from "
            f"the landing's best track, smoothed between samples.</p>"
            f'<script type="importmap">{_imports()}</script>'
            f'<script type="module" src="/static/replay.js"></script>')
    return _page(f"Replay: {p.pilot.name} {g.grade if g else ''}", f"<style>{REPLAY_STYLE}</style>{body}")


LIVE_STYLE = """
.replay .labels { position: absolute; inset: 0; pointer-events: none; overflow: hidden; }
.replay .labels .label { position: absolute; transform: translate(-50%, -100%); padding: 1px 6px; border-radius: 4px;
  background: rgba(0, 0, 0, 0.5); color: #e8edf2; font-size: 12px; white-space: nowrap; }
.replay .labels .label.followed { background: rgba(255, 208, 64, 0.85); color: #111; font-weight: 600; }
.replay-bar button[aria-pressed="mixed"] { border-style: dashed; border-color: var(--text); }
.live-status { color: var(--muted); }
.live-graded { margin: 8px 0; padding-left: 1.2em; }
tr.muted td, .muted { color: var(--muted); }
"""


def _imports() -> str:
    return json.dumps({"imports": {"three": f"{THREE_DIR}/three.module.min.js",
                                   "three/addons/controls/OrbitControls.js": f"{THREE_DIR}/OrbitControls.js"}})


def _scene_overlays(loading: str) -> str:
    """What's drawn over the 3D view: the PLAT monitor's frame, the HUD, the ball, the subtitle, and a loading line."""
    hud = "".join(f'<dt>{label}</dt><dd id="hud-{key}">–</dd>'
                  for key, label in (("range", "Range"), ("gs", "Glideslope"), ("lineup", "Lineup"), ("aoa", "AOA")))
    return ('<div class="platframe" aria-hidden="true">'
            # Laid out as on the deck camera's monitor (positions in a 1274 x 928 picture, as DCS's Supercarrier has them).
            + "".join(f'<span id="plat-{k}" style="left:{x}%;top:{y}%"></span>'
                      for k, x, y in (("date", 15.5, 1.1), ("time", 15.5, 8.6), ("status", 63.4, 1.1), ("hull", 76.8, 1.1),
                                      ("wind", 69.1, 8.6), ("speed", 15.5, 85.3), ("waveoff", 37.9, 85.3),
                                      ("distance", 50.0, 85.3), ("sink", 76.8, 85.3)))
            + "".join(f'<div class="line" id="plat-{k}"></div>' for k in ("left", "right", "top", "bottom"))
            + '</div>'
            f'<dl class="hud">{hud}</dl><div class="ballbox" id="hud-ballbox" hidden><div class="ball" id="hud-ball"></div></div>'
            f'<div class="call" id="hud-call" hidden></div><div class="loading">{escape(loading)}</div>')


LIVE_STATUS = {
    "live": "Live",
    "running": "Mission running, no carrier",
    "paused": "Mission paused (a dedicated server pauses with no players)",
    "silent": "No live data",
}


def _live_url(agent: str, unit: str | None = None) -> str:
    return f"/live/{quote(agent, safe='')}" + (f"/{quote(unit, safe='')}" if unit else "")


def _ago(when: datetime) -> str:
    seconds = (datetime.now(when.tzinfo) - when).total_seconds()
    if seconds < 90:
        return "just now"
    if seconds < 5400:
        return f"{round(seconds / 60)} min ago"
    if seconds < 36 * 3600:
        return f"{round(seconds / 3600)} h ago"
    return _when(when)


def _live_status(a) -> str:
    """A server agent's status (hub.live.AgentStatus) in words."""
    text = escape(LIVE_STATUS[a.status])
    if a.status == "silent":
        text += (" since the hub started" if a.heard is None
                 else f' <span class="muted">· last heard {escape(_ago(a.heard))}</span>')
    return text


def _live_carriers(a) -> str:
    """Links to a server's live carriers, with who's around each."""
    return "<br>".join(
        f'<a href="{_live_url(c.agent, c.unit)}">{escape(c.unit)}</a> · {len(c.pilots)} aircraft'
        + (f'<span class="muted"> ({", ".join(escape(p) for p in sorted(c.pilots.values(), key=str.lower))})</span>'
           if c.pilots else "")
        for c in a.carriers) or "–"


def live_index_page(agents) -> str:
    """Every server agent on the hub and its live view (hub.live.LiveBroker.status)."""
    if agents:
        rows = "".join(
            f'<tr class="{"" if a.status == "live" else "muted"}"><td><a href="{_live_url(a.agent)}">'
            f"{escape(a.agent)}</a></td><td>{_live_status(a)}</td><td>{_live_carriers(a)}</td></tr>"
            for a in agents)
        table = (f'<div class="scroll-x"><table class="board"><thead><tr><th>Server</th><th>Status</th>'
                 f"<th>Carriers</th></tr></thead><tbody>{rows}</tbody></table></div>")
    else:
        table = '<p class="sub">This hub has no server agents, so there\'s nothing to watch live.</p>'
    body = (_header("", "Watch the carriers on this hub's servers as it happens, from the LSO's view. A server "
                    "shows its carriers while its server agent is connected to a mission with one.")
            + f'<div class="panel">{table}</div>'

            '<script>setTimeout(() => location.reload(), 15000);</script>')
    return _page("Live", f"<style>{LIVE_STYLE}</style>{body}", "live")


def live_server_page(a) -> str:
    """One server agent's live view (hub.live.AgentStatus) when it isn't exactly one carrier to go straight to."""
    if a.carriers:
        what = (f'<div class="scroll-x"><table class="board"><thead><tr><th>Carriers</th></tr></thead>'
                f"<tbody><tr><td>{_live_carriers(a)}</td></tr></tbody></table></div>")
    else:
        what = f'<p class="sub">{_live_status(a)}. This page refreshes itself; a carrier shows here once one is live.</p>'
    body = (_header(escape(a.agent))
            + f'<div class="panel">{what}</div>'

            '<script>setTimeout(() => location.reload(), 15000);</script>')
    return _page(f"Live: {a.agent}", f"<style>{LIVE_STYLE}</style>{body}", "live")


def live_page(agent: str, unit: str, voice: bool = False) -> str:
    """One carrier's live view (static/live.js draws it from /live/{agent}/{unit}/events)."""
    src = _live_url(agent, unit) + "/events"
    body = (_header(escape(unit), f'Live from <a href="{_live_url(agent)}">{escape(agent)}</a> · '
                    f'<span class="live-status" id="live-status">Connecting…</span>')
            + 
            f'<div class="replay" id="live" data-src="{escape(src)}" data-voice="{1 if voice else 0}">'
            f'<canvas aria-label="Live 3D view of the carrier"></canvas><div class="labels"></div>'
            + _scene_overlays("Waiting for the carrier…") + '</div>'
            '<div class="replay-bar"><span class="group" id="live-follow" aria-label="Follow"></span>'
            '<button type="button" id="live-sound" aria-pressed="true" hidden>Sound on</button></div>'
            '<div class="replay-bar"><span class="group" id="live-views"></span></div>'
            '<ul class="live-graded" id="live-graded" hidden></ul>'
            '<p class="sub" id="live-hint"></p>'
            '<p class="sub">1–7 pick the camera and the mouse wheel zooms. Auto follows the jet in the groove, else the '
            "one nearest the ship. About a second behind the server; the jets and ship are simple stand-in models.</p>"
            f'<script type="importmap">{_imports()}</script>'
            '<script type="module" src="/static/live.js"></script>')
    return _page(f"Live: {unit}", f"<style>{REPLAY_STYLE}{LIVE_STYLE}</style>{body}", "live")


UPLOAD_SCRIPT = """
const form = document.getElementById('upload');
form.addEventListener('submit', (event) => {
  event.preventDefault();
  const status = document.getElementById('status');
  const bar = document.getElementById('bar');
  const data = new FormData();
  data.append('recording', form.elements.recording.files[0]);
  if (form.elements.debrief.files.length) data.append('debrief', form.elements.debrief.files[0]);
  const xhr = new XMLHttpRequest();
  xhr.open('POST', '/api/v1/recordings');
  bar.hidden = false;
  xhr.upload.onprogress = (e) => { if (e.lengthComputable) bar.value = e.loaded / e.total; };
  xhr.onload = () => {
    if (xhr.status === 202) { window.location = JSON.parse(xhr.responseText).page; return; }
    bar.hidden = true;
    let detail = xhr.responseText;
    try { detail = JSON.parse(xhr.responseText).detail; } catch (e) {}
    status.textContent = 'Upload failed (' + xhr.status + '): ' + detail;
  };
  xhr.onerror = () => { bar.hidden = true; status.textContent = 'Upload failed: network error'; };
  status.textContent = 'Uploading…';
  xhr.send(data);
});
"""


def upload_page(token_required: bool) -> str:
    """Upload a recording, signed in: your own passes in it. Signed out: a way to sign in. A hub that takes
    recordings only with a source's token (`token_required`) says so: those come through the API."""
    if token_required:
        body = (_header("Upload a recording", "This hub takes recordings from its servers only.")
                + '<div class="panel upload-panel"><p>Its servers send their recordings with their own upload '
                "token, through the API (<code>POST /api/v1/recordings</code>).</p></div>")
        return _page("Upload a recording", body, "upload")
    if not CURRENT_PILOT.get():
        body = (_header("Upload a recording", "Sign in to upload your recordings.")
                + '<div class="panel upload-panel"><h3>Sign in first</h3><p>Uploads are imported as your pilot, so '
                'the board needs to know who you are.</p><p class="actions"><a class="button dark" '
                'href="/signin?next=/upload">Sign in</a> <a class="button paper" href="/join">Join the board</a></p></div>')
        return _page("Upload a recording", body, "upload")
    body = (
        _header("Upload a recording", "Carrier passes are automatically extracted and added")
        + '<div class="panel upload-panel"><form class="upload" id="upload">'
        '<label>Tacview recording <input name="recording" type="file" accept=".acmi" required>'
        '<span class="hint">Required: a .zip.acmi or .txt.acmi file. Only your own passes in it are imported.'
        '</span></label>'
        '<label>debrief.log (optional) <input name="debrief" type="file" accept=".log">'
        "<span class=\"hint\">DCS's Logs/debrief.log from the same session, for DCS's own grades and wires "
        "(DCS overwrites it at the next mission).</span></label>"
        '<button type="submit">Upload recording ↗</button><progress id="bar" hidden value="0"></progress>'
        '<p id="status" class="error"></p></form></div>'
        f"<script>{UPLOAD_SCRIPT}</script>"
    )
    return _page("Upload a recording", body, "upload")


def upload_status_page(upload: Upload, key: str | None) -> str:
    """`key`: the uploader's own view (they may pick whose passes to import)."""
    running = upload.status in ("inspecting", "queued", "processing")
    refresh = '<meta http-equiv="refresh" content="3">' if running else ""
    rows = []
    for r in upload.results or []:
        kind = "own track" if r.get("kind") == "track" else (r.get("outcome") or "")
        if r.get("error"):
            result = f'<span class="error">{escape(r["error"])}</span>'
        elif r.get("grade"):
            state = "added" if r.get("created") else "already here"
            result = (f'<a href="/passes/{r["pass_id"]}">{escape(grade_name(r["grade"]))}: {escape(r.get("text") or "")}</a>'
                      f" ({state})")
        else:
            result = escape(r.get("text") or "kept until a report of this landing with the carrier arrives")
        start = r.get("start_time")
        rows.append(f"<tr><td>{escape(r.get('pilot') or '?')}</td><td>{escape(kind)}</td>"
                    f"<td>{'' if start is None else f'{start:.0f}s'}</td><td>{result}</td></tr>")
    if upload.status == "choose_pilot" and key:
        options = "".join(f'<option value="{escape(n)}">{escape(n)}</option>' for n in upload.pilots or [])
        detail = (f'<form class="upload" method="post" action="/uploads/{upload.id}/pilot">'
                  f'<input type="hidden" name="key" value="{escape(key)}">'
                  '<label>This recording has more than one own pilot. Which are you? <select name="pilot" required>'
                  f'<option value="" disabled selected>Choose…</option>{options}</select>'
                  '<span class="hint">Only that pilot\'s passes are imported.</span></label>'
                  + ('<label>Password (if that pilot has set one, and isn\'t you) <input name="password" '
                     'type="password" autocomplete="current-password"><span class="hint">Not needed for your own '
                     f'name: you\'re signed in as {escape(CURRENT_PILOT.get())}.</span></label>' if CURRENT_PILOT.get() else
                     '<label>Password (if that pilot has set one) <input name="password" type="password" '
                     'autocomplete="current-password"></label>')
                  + '<button type="submit">Import my passes</button></form>')
    elif upload.status == "needs_password" and key:
        wrong = f'<p class="error">{escape(upload.message)}</p>' if upload.message else ""
        detail = (f'<form class="upload" method="post" action="/uploads/{upload.id}/password">{wrong}'
                  f'<input type="hidden" name="key" value="{escape(key)}">'
                  f'<label>{escape(upload.pilot or "This pilot")} has set a password. Enter it to import their passes '
                  '<input name="password" type="password" required autocomplete="current-password"></label>'
                  '<button type="submit">Import</button></form>')
    elif upload.status == "needs_password":
        detail = '<p class="empty-state">Waiting for the pilot\'s password.</p>'
    elif upload.status == "choose_pilot":
        detail = '<p class="empty-state">Waiting for the uploader to choose their pilot.</p>'
    elif upload.status == "failed":
        detail = f'<p class="error">{escape(upload.message or "failed")}</p>'
    elif running:
        doing = {"inspecting": "Reading the recording", "queued": "Waiting to be processed"}.get(upload.status, "Processing")
        detail = f'<p class="empty-state">{doing}… (this page refreshes)</p>'
    elif rows:
        detail = ('<div class="scroll"><table class="results"><thead><tr><th>Pilot</th><th>Pass</th><th>At</th>'
                  f'<th>Result</th></tr></thead><tbody>{"".join(rows)}</tbody></table></div>')
    else:
        detail = f'<p class="empty-state">{escape(upload.message or "No carrier passes or approaches found in this recording.")}</p>'
    sub = (f'{upload.size / 1e6:.1f} MB · '
           f'{escape(f"passes flown by {upload.pilot}" if upload.pilot else f"from {upload.source.name}")} · '
           f'{escape(upload.status)}')
    body = (_header(escape(upload.filename), sub,
                    actions='<a class="button paper" href="/upload">Upload another</a>')
            + f'<div class="panel">{detail}</div>')
    return _page(f"Upload: {upload.filename}", body, "upload").replace("<head>", "<head>" + refresh, 1)


# Hovering a pass's grade square previews its trap card next to the pointer (not the overlay's lines).
CARD_PREVIEW_SCRIPT = """
(() => {
  const pop = document.getElementById('tc-pop');
  const cards = new Map();
  let current = null;
  const place = (e) => {
    const w = pop.offsetWidth, h = pop.offsetHeight, m = 16;
    let x = e.clientX + m, y = e.clientY + m;
    if (x + w > window.innerWidth - 8) x = Math.max(8, e.clientX - w - m);
    if (y + h > window.innerHeight - 8) y = Math.max(8, window.innerHeight - h - 8);
    pop.style.left = x + 'px'; pop.style.top = y + 'px';
  };
  const show = async (id, e) => {
    current = id;
    pop.hidden = false;
    if (!cards.has(id)) {
      pop.innerHTML = '<div class="loading">Loading trap card…</div>';
      place(e);
      cards.set(id, fetch('/passes/' + id + '/card.svg').then(r => r.ok ? r.text() : '<div class="loading">No trap card</div>'));
    }
    const svg = await cards.get(id);
    if (current !== id) return;
    pop.innerHTML = svg;
    place(e);
  };
  const hide = () => { current = null; pop.hidden = true; };
  document.querySelectorAll('[data-pass]').forEach((el) => {
    const id = el.getAttribute('data-pass');
    el.addEventListener('mouseenter', (e) => show(id, e));
    el.addEventListener('mousemove', (e) => { if (current === id) place(e); });
    el.addEventListener('mouseleave', hide);
  });
})();
"""

# Drag across the overlay to zoom into that stretch of the approach (redrawn by the server, so the scales
# and labels adapt); "Reset zoom" or a double-click goes back to the whole approach.
# Drag across a chart (the trap card, or the pilot's traps overlaid) to zoom into that stretch of the approach;
# double-click or the reset button to see it all again. Each `.zoomable` box has `data-src` (its SVG's URL,
# which takes `near`/`far`) and `data-reset` (its reset button's id); its SVG carries its current view.
ZOOM_SCRIPT = """
(() => {
  for (const box of document.querySelectorAll('.zoomable')) {
    const reset = document.getElementById(box.dataset.reset);
    const sel = document.createElement('div');
    sel.className = 'zoom-sel';
    sel.hidden = true;
    box.appendChild(sel);
    let drag = null, dragged = false;
    const along = (svg, clientX) => {
      const r = svg.getBoundingClientRect();
      const px = (clientX - r.left) * svg.viewBox.baseVal.width / r.width;
      const near = +svg.dataset.near, far = +svg.dataset.far;
      const f = Math.min(1, Math.max(0, (px - +svg.dataset.padL) / +svg.dataset.plotW));
      return far - f * (far - near);
    };
    const load = async (near, far) => {
      const url = new URL(box.dataset.src, location.href);
      if (near === null) {
        url.searchParams.delete('near');
        url.searchParams.delete('far');
      } else {
        url.searchParams.set('near', near.toFixed(1));
        url.searchParams.set('far', far.toFixed(1));
      }
      box.classList.add('loading');
      try {
        const r = await fetch(url);
        if (r.ok) {
          box.querySelector('svg').outerHTML = await r.text();
          if (reset) reset.hidden = near === null;
        }
      } finally {
        box.classList.remove('loading');
      }
    };
    box.addEventListener('mousedown', (e) => {
      const svg = box.querySelector('svg');
      if (e.button !== 0 || !svg) return;
      drag = { x: e.clientX, svg };
      dragged = false;
      e.preventDefault();
    });
    window.addEventListener('mousemove', (e) => {
      if (!drag) return;
      if (Math.abs(e.clientX - drag.x) > 4) dragged = true;
      if (!dragged) return;
      const r = box.getBoundingClientRect();
      sel.hidden = false;
      sel.style.left = (Math.min(drag.x, e.clientX) - r.left) + 'px';
      sel.style.width = Math.abs(e.clientX - drag.x) + 'px';
    });
    window.addEventListener('mouseup', (e) => {
      if (!drag) return;
      const d = drag;
      drag = null;
      sel.hidden = true;
      if (!dragged) return;
      const a = along(d.svg, d.x), b = along(d.svg, e.clientX);
      load(Math.min(a, b), Math.max(a, b));
    });
    // A drag that ends on a line isn't a click on it.
    box.addEventListener('click', (e) => { if (dragged) { e.preventDefault(); e.stopPropagation(); dragged = false; } }, true);
    box.addEventListener('dblclick', () => load(null, null));
    if (reset) reset.addEventListener('click', () => load(null, null));
  }
})();
"""

STEPPER_SCRIPT = """
(() => {
  // Look at last N: − and + step it; typing a number and leaving the box (or Enter) loads it. Kept in range.
  document.querySelectorAll('.stepper').forEach((box) => {
    const input = box.querySelector('input');
    const go = () => {
      const lo = +input.min, hi = +input.max, n = Math.round(+input.value);
      input.value = Math.min(hi, Math.max(lo, isNaN(n) ? lo : n));
      input.form.submit();
    };
    input.addEventListener('change', go);
    box.querySelectorAll('button[data-step]').forEach((b) => b.addEventListener('click', () => {
      input.value = +input.value + +b.dataset.step;
      go();
    }));
  });
})();
"""

KIND_LABELS = {"fault": "Fault", "bias": "Leaning", "speed": "Speed", "outcome": "Outcome", "wires": "Wires",
               "trend": "Trend"}


def pilot_page(name: str, summary, passes: int, overlay_svg: str | None = None, overlay_src: str = "",
               servers: str = "all") -> str:
    """Meta grading: themes across the pilot's recent passes (`summary`: the hub's PilotSummary)."""
    result, rows = summary.trends, summary.rows
    first = summary.first_seen.strftime("%Y-%m-%d") if summary.first_seen else "?"
    last = summary.last_seen.strftime("%Y-%m-%d") if summary.last_seen else "?"
    quoted = quote(name, safe="")
    title = escape(name) + (f' <span class="modex">#{escape(summary.modex)}</span>' if summary.modex else "")
    chosen = summary.airframe or ""
    sub = f"first seen {first} · last seen {last}"
    heading = _header(title, sub)
    tabs = ""
    if summary.airframes:  # trends don't mix airframes: one at a time (a single tab says which it is)
        def tab(a: str, n: int) -> str:
            href = f"?passes={passes}&servers={servers}&aircraft={quote(a, safe='')}"
            current = ' class="current" aria-current="page"' if a == chosen else ""
            return f'<a href="{escape(href)}"{current}>{escape(a)} <span class="n">{n}</span></a>'
        tabs = '<nav class="airframes">' + "".join(tab(a, n) for a, n in summary.airframes) + "</nav>"
    picker = (f'<form class="filters compact" method="get"><input type="hidden" name="aircraft" value="{escape(chosen)}">'
              '<div class="field"><span id="look-at">Look at last</span><div class="stepper">'
              '<button type="button" data-step="-1" aria-label="Fewer passes">−</button>'
              f'<input name="passes" type="number" min="3" max="50" step="1" value="{passes}" aria-labelledby="look-at" '
              '><button type="button" data-step="1" aria-label="More passes">+</button>'
              f"</div></div><label>Servers <select name=\"servers\" onchange=\"this.form.submit()\">"
              + "".join(f'<option value="{v}"{" selected" if v == servers else ""}>{label}</option>'
                        for v, label in (("ours", "This hub&#x27;s servers"), ("all", "All servers")))
              + f"</select></label></form><script>{STEPPER_SCRIPT}</script>")
    if not result.passes:
        body = (f'{heading}{tabs}{picker}'
                f'<p class="empty-state">No graded {escape(chosen + " " if chosen else "")}passes yet.</p>')
        return _page(name, body, _pilot_section(name))
    n = result.passes
    bolters = sum(p.outcome == "bolter" for p in rows)
    traps = [p for p in rows if p.outcome == "trap"]
    def wire(p) -> int | None:
        return p.wire if p.wire is not None else ((p.grade.detail or {}) if p.grade else {}).get("wire_estimate")
    short = sum(wire(p) in (1, 2) for p in traps)
    stats = ('<div class="stats">'
             f'<div><div class="micro quiet">Average grade</div><strong>{result.average_points:.2f}</strong></div>'
             f'<div><div class="micro quiet">Bolters</div><strong>{bolters} / {n}</strong></div>'
             f'<div><div class="micro quiet">Short traps</div><strong>{short} / {len(traps)}</strong></div></div>')

    def faults(themes, empty: str) -> str:
        return "".join(
            f'<div class="fault"><div><strong>{escape(t.text)}</strong>'
            f'<small>{KIND_LABELS.get(t.kind, t.kind)}</small></div><b>{t.count} / {t.of}</b></div>'
            for t in themes) or f'<div class="fault none">{empty}</div>'

    analysis = faults(result.analysis, "Nothing stands out: no fault or leaning repeats across these passes.")
    results = faults(result.results, "No pattern in outcomes or wires across these passes.")
    strengths = ('<h3 style="margin-top:22px">Consistently good</h3><ul class="themes">'
                 + "".join(f"<li>{escape(s)}</li>" for s in result.strengths) + "</ul>") if result.strengths else ""
    cells = "".join(
        f'<a class="cell {GRADE_CLASS.get(p.grade.grade, "")}" href="/passes/{p.id}" data-pass="{p.id}" '
        f'title="{escape(grade_name(p.grade.grade))}: {escape(p.grade.text)} · {_when(p.occurred_at)}">'
        f"{escape(grade_short(p.grade.grade))}</a>" for p in reversed(rows))
    grades = " · ".join(f"{escape(grade_short(g))} ×{k}" for g, k in sorted(result.grades.items(), key=lambda kv: -kv[1]))
    body = (heading + tabs + picker + stats
            + '<div class="section-header"><div>'
            "<h2>Pilot themes</h2></div><p>Passes shown oldest → newest</p></div>"
            '<div class="insight-grid"><div class="panel"><div class="micro quiet">Recurring faults</div>'
            f"{analysis}{strengths}</div>"
            f'<div class="panel"><div class="micro quiet">Grade sequence</div><div class="small-grades recent">{cells}</div>'
            f'<p>{grades}</p><div class="micro quiet" style="margin-top:18px">Results</div>{results}</div></div>'
            + (f'<div class="section-header"><div><h2>Traps overlaid <button id="zoom-reset" class="zoom-reset" hidden>Reset zoom</button></h2></div>'
               f"<p>Last {n} · drag across a chart to zoom</p></div>"
               f'<div class="panel card zoomable" id="overlay" data-src="{escape(overlay_src)}" data-reset="zoom-reset">'
               f'{overlay_svg}</div>'
               f"<script>{ZOOM_SCRIPT}</script>" if overlay_svg else "")
            + f'<div id="tc-pop" hidden></div><script>{CARD_PREVIEW_SCRIPT}</script>')
    return _page(f"{name}: recent passes", body, _pilot_section(name))


def waiting_page(name: str) -> str:
    """For a signed-in pilot an admin hasn't approved yet, where only approved pilots may go."""
    body = (_header("Waiting for approval.")
            + 
            f'<div class="panel"><p>An admin approves new pilots on this board, and hasn\'t approved {escape(name)} yet. '
            f'This opens once they do. <a href="/pilots/{quote(name, safe="")}/settings">Your settings</a></p></div>')
    return _page("Waiting for approval", body)


def join_page(error: str | None = None, name: str = "", existing: str | None = None, approval: bool = False) -> str:
    """Join the board before flying here: choose a name and a password (`approval`: an admin approves new pilots)."""
    note = f'<p class="error">{escape(error)}</p>' if error else ""
    if existing:
        note = (f'<p class="error">{escape(existing)} is already on this board. If that\'s you, '
                f'<a href="/pilots/{quote(existing, safe="")}/settings">set your password on your settings page</a>.</p>')
    form = (
        '<form class="upload" method="post" action="/join">'
        '<p class="sub">Choose the name you fly under in DCS and a password. With it you can create pilot tokens '
        "(to send your passes here from your own PC) and claim other names you fly under. Passes reported under "
        "this name count as yours.</p>"
        + ('<p class="sub">An admin approves new pilots on this board: until they do, you can sign in and see that '
           "you're waiting, nothing more.</p>" if approval else "") +
        f'<label>Pilot name <input name="name" required maxlength="100" value="{escape(name)}" autocomplete="username"></label>'
        '<label>Password <input name="password" type="password" required minlength="8" autocomplete="new-password">'
        '<span class="hint">At least 8 characters</span></label>'
        '<label>Password again <input name="confirm" type="password" required minlength="8" autocomplete="new-password">'
        "</label><button type=\"submit\">Join</button></form>")
    aside = ('<aside class="panel"><div class="micro quiet">Already flying here?</div><h3>Sign in</h3>'
             "<p>If you've joined before, or an admin set a password for your name, sign in instead.</p>"
             '<a class="button paper" href="/signin">Sign in</a></aside>')
    body = (_header("Join the board.", "Choose your pilot name and a password.")
            + f'{note}<div class="upload-grid"><div class="panel"><div class="micro quiet">New pilot</div>{form}</div>{aside}</div>')
    return _page("Join this board", body)


def signin_page(error: str | None = None, name: str = "", next_url: str = "", members_only: bool = False) -> str:
    note = f'<p class="error">{escape(error)}</p>' if error else ""
    if members_only:
        note = ('<p class="sub" style="color:var(--text)">This board is for its members: sign in to see it, or '
                '<a href="/join">join</a>.</p>' + note)
    form = (
        '<form class="upload" method="post" action="/signin">'
        f'<input type="hidden" name="next" value="{escape(next_url)}">'
        f'<label>Pilot name <input name="name" required maxlength="100" value="{escape(name)}" autocomplete="username">'
        '<span class="hint">The name you fly under (or one of your other names).</span></label>'
        '<label>Password <input name="password" type="password" required autocomplete="current-password"></label>'
        '<button type="submit">Sign in</button></form>')
    aside = ('<aside class="panel"><div class="micro quiet">New pilot</div><h3>Join the board</h3>'
             "<p>Join to create pilot tokens, so the pilot hook sends your passes here, and to claim the other names "
             'you fly under.</p><a class="button paper" href="/join">Join the board</a></aside>')
    body = (f'{note}<div class="upload-grid"><div class="panel"><div class="micro quiet">Pilot access</div>{form}'
            '<p class="sub" style="margin:16px 0 0">Forgot your password? Ask the server\'s admin to reset it.</p></div>'
            f"{aside}</div>")
    return _page("Sign in", body)


def pilot_settings_page(pilot, done: str | None, error: str | None, tokens: list | None = None,
                        aliases: list | None = None, new_token: str | None = None, signed_in: bool = False,
                        claimable: list[str] | None = None) -> str:
    """Set or change the pilot's password; signed in as them (`signed_in`), change their side number, create
    and revoke pilot tokens, and claim other in-game names. `new_token`: a token just created, shown this once."""
    name = pilot.name
    quoted = quote(name, safe="")
    note = (f'<p class="sub" style="color:var(--text)">{escape(done)}</p>' if done else "") + (
        f'<p class="error">{escape(error)}</p>' if error else "")
    has = pilot.password_hash is not None
    back = _header("User Settings")
    if has and not signed_in:
        me = CURRENT_PILOT.get()
        other = f" You're signed in as {escape(me)}." if me else ""
        body = (f'{back}{note}<div class="panel"><p>Sign in as {escape(name)} to change these settings.{other}</p>'
                f'<p><a href="/signin?next={quote(f"/pilots/{quoted}/settings", safe="")}">Sign in</a></p></div>'
                '<p class="sub" style="margin-top:12px">Forgot your password? Ask the server\'s admin to reset it.</p>')
        return _page(f"{name}: settings", body, _pilot_section(name, "settings", None))
    password_form = (
        f'<form class="upload" method="post" action="/pilots/{quoted}/settings/password">'
        "<h2 style=\"margin-top:0\">" + ("Change password" if has else "Set a password") + "</h2>"
        + ("" if has else '<p class="sub">Nobody has set one for this pilot yet: setting it claims the name and signs '
           "you in. Recordings uploaded without a token then need it to import this pilot's passes.</p>")
        + ('<label>Current password <input name="current" type="password" required autocomplete="current-password">'
           '<span class="hint">Changing it signs you out everywhere else</span></label>' if has else "")
        + '<label>New password <input name="new" type="password" required minlength="8" autocomplete="new-password">'
        '<span class="hint">At least 8 characters</span></label>'
        '<label>New password again <input name="confirm" type="password" required minlength="8" '
        'autocomplete="new-password"></label><button type="submit">Save password</button></form>')
    if not has:  # nothing else until the name is claimed
        body = f'{back}{note}<div class="panel">{password_form}</div>'
        return _page(f"{name}: settings", body, _pilot_section(name, "settings", None))
    if pilot.pending_since is not None:  # nothing else until an admin approves them
        waiting = ('<div class="panel"><h2 style="margin-top:0">Waiting for approval</h2><p>An admin approves new '
                   f"pilots on this board, and hasn't approved {escape(name)} yet (asked {escape(_when(pilot.pending_since))}). "
                   "Until then you can't create pilot tokens, claim other names, or change your side number, and "
                   "your password doesn't protect uploads of your passes yet. Your passes still reach the board.</p></div>")
        body = f'{back}{note}{waiting}<div class="panel" style="margin-top:12px">{password_form}</div>'
        return _page(f"{name}: settings", body, _pilot_section(name, "settings", None))
    modex_form = (
        f'<form class="upload" method="post" action="/pilots/{quoted}/settings/modex">'
        '<h2>Side number</h2>'
        '<label><input name="modex" aria-label="Side number" required pattern="[0-9]{1,4}" inputmode="numeric" '
        f'value="{escape(pilot.modex or "")}"></label>'
        '<button type="submit">Save side number</button></form>')
    shown = (f'<div class="token-once"><p><strong>Your new pilot token</strong> (copy it now: it won\'t be shown again)</p>'
             f'<code>{escape(new_token)}</code></div>' if new_token else "")
    rows = "".join(
        f'<tr><td>{escape(t.label or t.name)}</td><td>{_when(t.created_at)}</td>'
        f'<td>{_when(t.last_used_at) if t.last_used_at else "never"}</td><td>'
        + ("revoked" if t.revoked_at else
           f'<form method="post" action="/pilots/{quoted}/settings/tokens/{t.id}/revoke" class="inline">'
           '<button type="submit">Revoke</button></form>')
        + "</td></tr>" for t in tokens or [])
    # The list (each Revoke its own form) goes after the create form, not in it: a form inside a form is dropped by
    # browsers, and Revoke would submit the create form.
    listed = (f'<div class="scroll" style="margin-top:12px"><table class="results"><thead><tr><th>Token</th>'
              f'<th>Created</th><th>Last used</th><th></th></tr></thead><tbody>{rows}</tbody></table></div>'
              if rows else "")
    tokens_form = (
        f'<form class="upload" method="post" action="/pilots/{quoted}/settings/tokens"><h2>Pilot tokens'
        + _help_icon("A pilot token lets the pilot hook send your passes here.") + "</h2>" + shown
        + '<label>What it\'s for <input name="label" maxlength="100" placeholder="e.g. my PC"></label>'
        '<button type="submit">Create a pilot token</button></form>' + listed)
    names = "".join(f'<li>{escape(a.name)}{"" if a.claimed else " (seen with your pilot token)"}</li>'
                    for a in aliases or [])
    aliases_form = (
        f'<form class="upload" method="post" action="/pilots/{quoted}/settings/aliases"><h2>Other names'
        + _help_icon("Other in-game names you fly under (e.g. with a squadron tag). Passes reported under them "
                     "count as yours.") + "</h2>"
        + (f'<ul class="themes">{names}</ul>' if names else '<p class="sub">None yet</p>')
        + ('<label><span>Claim a name' + _help_icon("Only a name nobody else has claimed. Its passes on this board "
                                                    "move to you.")
           + '</span><select name="alias" required><option value="">Choose a name</option>'
           + "".join(f'<option value="{escape(n)}">{escape(n)}</option>' for n in claimable)
           + '</select></label><button type="submit">Claim name</button>'
           if claimable else '<p class="sub">No names on the board to claim</p>')
        + "</form>")
    body = (f'{back}{note}<div class="panel">{password_form}</div><div class="panel" style="margin-top:12px">{modex_form}</div>'
            f'<div class="panel" style="margin-top:12px">{tokens_form}</div>'
            f'<div class="panel" style="margin-top:12px">{aliases_form}</div>')
    return _page(f"{name}: settings", body, _pilot_section(name, "settings", None))


def admin_page(pilots: list, agents: list, q: str = "", done: str | None = None, error: str | None = None,
               new_token: tuple[str, str] | None = None, css: str = "", telemetry: dict | None = None,
               pending: list | None = None, approval: bool = False, members_only: bool = False,
               settings: dict | None = None, voices: list | None = None) -> str:
    """For admins (pilots with the role, signed in): pilots (admin role, password reset, removal, other names,
    pilot tokens), server agents (adding one, their configuration), and the hub settings (`settings`, by key; `voices`:
    the clip sets the hub has, for the default voice). `new_token`: (what, token), shown once."""
    me = CURRENT_PILOT.get()
    note = (f'<p class="sub" style="color:var(--text)">{escape(done)}</p>' if done else "") + (
        f'<p class="error">{escape(error)}</p>' if error else "")
    if new_token:
        note += (f'<div class="token-once"><p><strong>The {escape(new_token[0])}</strong> (copy it now: it won\'t be '
                 f'shown again)</p><code>{escape(new_token[1])}</code></div>')
    keep = f'<input type="hidden" name="q" value="{escape(q)}">'

    def post(action: str, label: str, confirm: str = "", danger: bool = False, extra: str = "") -> str:
        ask = f' onsubmit="return confirm(\'{confirm}\')"' if confirm else ""
        cls = ' class="danger"' if danger else ""
        return (f'<form method="post" action="{escape(action)}"{ask}>{keep}{extra}'
                f'<button type="submit"{cls}>{label}</button></form>')

    rows = []
    for p in pilots:
        quoted = quote(p.name, safe="")
        base = f"/admin/pilots/{quoted}"
        acts = []
        if p.admin:
            if p.name != me:
                acts.append(post(f"{base}/admin", "Remove admin", extra='<input type="hidden" name="admin" value="0">'))
        elif p.has_password and not p.pending:
            acts.append(post(f"{base}/admin", "Make admin", "Make this pilot an admin?",
                             extra='<input type="hidden" name="admin" value="1">'))
        if p.has_password and not p.pending and p.name != me:
            acts.append(post(f"{base}/reset-password", "Reset password",
                             "Clear this pilot\\'s password? They (or anyone) can then set a new one.", danger=True))
        if not p.admin:
            lost = (f" and their {p.passes} landing{'s' if p.passes != 1 else ''} (gone from the board for good)"
                    if p.passes else "")
            acts.append(post(f"{base}/remove", "Delete",
                             f"Delete {_js_text(p.name)}{lost}? Their pilot tokens are revoked. "
                             "If they fly here again, they come back as a new pilot.", danger=True))
        if not p.pending:  # approve them first (Approving new pilots, above)
            acts.append(post(f"{base}/tokens", "New pilot token",
                             extra='<input class="label" name="label" maxlength="100" placeholder="for (e.g. PC)">'))
        names = "".join(
            f'<span class="alias">{escape(a)}'
            + post("/admin/aliases/remove", "×", "Remove this alias? Its passes go back to a pilot of that name.",
                   extra=f'<input type="hidden" name="alias" value="{escape(a)}">') + "</span>" for a in p.aliases)
        landed = p.last_landed
        rows.append(
            f'<tr><td data-value="{escape(p.name.lower())}"><a href="/pilots/{quoted}">{escape(p.name)}</a>'
            + (' <span class="tag">admin</span>' if p.admin else "")
            + (' <span class="tag">waiting</span>' if p.pending else "") + "</td>"
            f'<td data-value="{p.passes}">{p.passes}</td>'
            f'<td data-value="{1 if p.has_password else 0}">{"set" if p.has_password else "none"}</td>'
            f'<td data-value="{landed.timestamp() if landed else 0}">{_when(landed) if landed else "never"}</td>'
            f'<td>{names or "<span class=sub>none</span>"}</td><td><div class="actions">{"".join(acts)}</div></td></tr>')
    pilots_table = (
        '<div class="scroll"><table class="results" data-sortable><thead><tr>'
        + "".join(f'<th data-sort="{kind}"><button type="button">{label}</button></th>'
                  for label, kind in (("Pilot", "text"), ("Passes", "num"), ("Password", "num"), ("Last landed", "num")))
        + f'<th>Other names</th><th></th></tr></thead><tbody>{"".join(rows)}</tbody></table></div>'
        f"<script>{SORT_SCRIPT}</script>"
        if rows else '<p class="sub">No pilots' + (" match." if q else " yet.") + "</p>")
    search = ('<form method="get" action="/admin" class="actions" style="margin-bottom:8px">'
              f'<input name="q" value="{escape(q)}" placeholder="Pilot or other name"><button type="submit">Find</button>'
              + ('<a href="/admin">Show all</a>' if q else "") + "</form>")
    agent_rows = "".join(
        f'<tr><td><a href="/admin/agents/{quote(a.name, safe="")}">{escape(a.name)}</a></td><td>{_when(a.created_at)}</td>'
        f'<td>{_when(a.last_used_at) if a.last_used_at else "never"}</td>'
        f'<td><div class="actions"><a href="/admin/agents/{quote(a.name, safe="")}">Settings</a>'
        + post(f"/admin/agents/{quote(a.name, safe='')}/remove", "Remove",
               f"Remove the server agent {_js_text(a.name)}? Its token stops working; its landings stay on the board.",
               danger=True)
        + "</div></td></tr>" for a in agents)
    agents_table = (
        '<div class="scroll"><table class="results"><thead><tr><th>Server agent</th><th>Added</th><th>Last used</th>'
        f'<th></th></tr></thead><tbody>{agent_rows}</tbody></table></div>' if agent_rows
        else '<p class="sub">None yet</p>')
    add_agent = ('<form method="post" action="/admin/agents" class="actions" style="margin-top:8px">'
                 '<input name="name" required maxlength="100" placeholder="e.g. server1">'
                 '<button type="submit">Add a server agent</button></form>')
    tel = telemetry or {}
    usage_link = ' · <a href="/admin/usage">Usage</a>' if tel.get("view") else ""
    body = (_header("", "Regrading and the wire check are run on the hub's machine "
                    f"(<code>burble hub regrade</code>, <code>burble hub wire-check</code>).{usage_link}")
            + f'{note}{_telemetry_notice(tel)}<div class="admin">{_waiting(pending or [], approval, post)}'
            f'{_hub_settings(settings or {}, voices or [], _access(members_only, approval))}'
            f'<div class="panel"><h2 style="margin-top:0">Pilots</h2>{search}{pilots_table}</div>'
            f'<div class="panel" style="margin-top:12px"><h2 style="margin-top:0">Server agents</h2>{agents_table}'
            f"{add_agent}</div>{_appearance(css)}{_telemetry_panel(tel) if telemetry else ''}</div>")
    return _page("Admin", body, "admin")


def agent_settings_page(agent, done: str | None = None, error: str | None = None,
                        form: dict[str, list[str]] | None = None) -> str:
    """A server agent's settings (PLAN #43): the common ones as a form (agentconfig.SECTIONS), its carriers'
    frequencies, which calls it makes, and its call thresholds; the raw JSON below for anything else. `form`: what
    was just posted (shown again after an error, so nothing typed is lost)."""
    from .. import agentconfig as ac

    config = agent.config if isinstance(agent.config, dict) else {}
    info = agent.agent_info or {}
    typed = form is not None and "raw" not in form
    name = quote(agent.name, safe="")

    def typed_value(key: str) -> str:
        return ((form or {}).get(key) or [""])[-1]

    def shown(value: object) -> str:
        if value is None:
            return ""
        return value if isinstance(value, str) else json.dumps(value)

    def field(setting) -> str:
        key = ac.field_name(setting)
        current = ac.get(config, setting.path)
        hint = f'<span class="hint">{escape(setting.help)}</span>' if setting.help else ""
        if setting.kind == "bool":
            on = key in (form or {}) if typed else (current if isinstance(current, bool) else setting.default)
            control = f'<input type="checkbox" name="{key}" value="1"{" checked" if on else ""}>'
        elif setting.kind == "choice" or setting.path == ("callouts", "voice") and info:
            choices = list(setting.choices)
            if setting.path == ("callouts", "voice"):
                voices = info.get("voices") or []
                choices = [("", "First by name")] + [(v, v) for v in voices] + (
                    [(EACH_CARRIER, "A different one for each carrier")] if len(voices) > 1 else [])
            value = typed_value(key) if typed else shown(current if current is not None else setting.default)
            if setting.path[-1] == "case" and value.upper() in ("I", "III"):
                value = value.upper()
            if value not in [str(c) for c, _ in choices]:
                choices.append((value, f"{value} (not on the agent)" if setting.path[-1] == "voice" else value))
            control = f'<select name="{key}">' + "".join(
                f'<option value="{escape(str(c))}"{" selected" if str(c) == value else ""}>{escape(label)}</option>'
                for c, label in choices) + "</select>"
        else:
            value = typed_value(key) if typed else shown(current)
            kind = ' type="number" step="any"' if setting.kind in ("int", "number") else ""
            default = f"{setting.default:g}" if isinstance(setting.default, float) else setting.default
            placeholder = "" if default is None else f' placeholder="{escape(str(default))}"'
            control = f'<input name="{key}"{kind} value="{escape(value)}"{placeholder}>'
        return _setting_row(key, escape(setting.label), control, hint)

    sections = []
    for title, group in ac.SECTIONS:
        sections.append(f"<h2>{escape(title)}</h2>" + "".join(field(st) for st in group))
        if title == "Live calls":
            sections.append(_carrier_rows(config, form if typed else None, info))
        if title == "Listening and recording":
            if (ac.get(config, ("callouts", "listen")) is True and info and not info.get("listen_model")):
                sections.append('<p class="warn">Listening is on, but this agent has no listen_model, '
                                "so it can't hear anyone.</p>")

    calls = ac.get(config, ("callouts", "calls"))
    chosen = set(form.get("call") or []) if typed else (set(calls) if isinstance(calls, list) else None)
    groups = "".join(
        f"<fieldset><legend>{escape(title)}</legend>" + "".join(
            f'<label><input type="checkbox" name="call" value="{escape(c.value)}"'
            f'{" checked" if chosen is None or c.value in chosen else ""}> {escape(ac.call_label(c))}'
            + (f' <span class="note">({escape(ac.CALL_NOTES[c])})</span>' if c in ac.CALL_NOTES else "") + "</label>"
            for c in group) + "</fieldset>" for title, group in ac.CALL_GROUPS)

    set_thresholds = ac.get(config, ("callouts", "thresholds"))
    set_thresholds = set_thresholds if isinstance(set_thresholds, dict) else {}
    rows = []
    for t in ac.thresholds():
        key = f"threshold.{t.name}"
        value = typed_value(key) if typed else shown(set_thresholds.get(t.name))
        rows.append(f'<tr><td><code>{t.name}</code></td><td><input name="{key}" type="number" step="any" '
                    f'value="{escape(value)}" placeholder="{t.default:g}" aria-label="{t.name}"></td>'
                    f'<td class="default">{escape(ac.show_number(t.default, t.unit))}</td>'
                    f'<td class="note">{escape(t.note)}</td></tr>')
    changed = any(t.name in set_thresholds for t in ac.thresholds())
    thresholds = (f'<details{" open" if changed else ""}><summary><strong>Call thresholds</strong> (advanced; '
                  f"{sum(t.name in set_thresholds for t in ac.thresholds())} changed)</summary>"
                  '<p class="sub">Leave one empty for its default. Distances are in metres (1 nm = 1852 m), angles in '
                  "degrees. The AOA band is each aircraft's own.</p>"
                  '<div class="scroll"><table class="results"><thead><tr><th>Threshold</th><th>Value</th><th>Default</th>'
                  f'<th></th></tr></thead><tbody>{"".join(rows)}</tbody></table></div></details>')

    if info:
        voices = info.get("voices") or []
        about = (f"Agent version {escape(info.get('version') or 'unknown')}; "
                 + (f"one clip set ({escape(voices[0])}), used whatever the voice says" if info.get("one_voice") and voices
                    else f"clip sets: {escape(', '.join(voices))}" if voices else "no voice clips (it can't make calls)")
                 + f"; listening model: {'yes' if info.get('listen_model') else 'no'}. "
                 f"(Reported {escape(str(info.get('at', ''))[:16].replace('T', ' '))} UTC.)")
    else:
        about = ("This agent hasn't said which voices it has yet: it does when it next fetches its settings (agents "
                 "older than this hub don't). Type the voice's folder name meanwhile.")
    raw_value = typed_value("raw") if form is not None and "raw" in form else (
        json.dumps(config, indent=2) if config else "")
    note = (f'<p class="sub" style="color:var(--text)">{escape(done)}</p>' if done else "") + (
        f'<p class="error">{escape(error)}</p>' if error else "")
    body = (_header(f"{escape(agent.name)}: settings.", "Its server agent fetches these from the hub and applies them "
                    f"when the next mission starts. {about}")
            + f"{note}"
            f'<div class="admin settings"><form class="panel" method="post" action="/admin/agents/{name}/settings">'
            + "".join(sections)
            + "<h2>Calls</h2><p class=\"sub\">Which calls the LSO makes.</p>" + groups
            + f'<h2>Thresholds</h2>{thresholds}<p><button type="submit">Save settings</button></p></form>'
            f'<div class="panel" style="margin-top:12px"><details{" open" if form is not None and "raw" in form else ""}>'
            '<summary><strong>Raw configuration (JSON)</strong></summary>'
            '<p class="sub">Everything the agent is sent, including keys the form doesn\'t show (kept when the form '
            "is saved). Checked before it's saved. Every key is listed by <code>burble hub config-reference</code>.</p>"
            f'<form method="post" action="/admin/agents/{name}/config">'
            f'<textarea name="config" spellcheck="false" rows="16">{escape(raw_value)}</textarea>'
            '<button type="submit">Save configuration</button></form></details></div></div>')
    return _page(f"{agent.name}: settings", body, "admin")


def _carrier_rows(config: dict, form: dict[str, list[str]] | None, info: dict | None = None) -> str:
    """Per-carrier frequencies and voices (callouts.carriers), with two empty rows to add more. `info`: what the agent
    said about itself (its clip sets, for the voice)."""
    from .. import agentconfig as ac

    keys = ("unit", "frequency_mhz", "modulation", "voice")
    if form is not None:
        n = int((form.get("carriers") or ["0"])[-1] or 0)
        rows = [tuple((form.get(f"carrier.{i}.{k}") or [""])[-1] for k in keys) for i in range(n)]
        rows = [r for r in rows if r[0] or r[1] or r[3]]
    else:
        carriers = ac.get(config, ("callouts", "carriers"))
        rows = [(unit, f"{radio.get('frequency_mhz', '')}", str(radio.get("modulation", "AM")), str(radio.get("voice", "")))
                for unit, radio in (carriers.items() if isinstance(carriers, dict) else []) if isinstance(radio, dict)]
    rows += [("", "", "AM", "")] * 2
    voices = (info or {}).get("voices") or []

    def voice_cell(i: int, voice: str) -> str:
        if not voices or (info or {}).get("one_voice"):
            return (f'<input name="carrier.{i}.voice" value="{escape(voice)}" placeholder="the LSO\'s" '
                    'aria-label="Voice (clip set)">')
        choices = [("", "The LSO's voice")] + [(v, v) for v in voices]
        if voice not in [c for c, _ in choices]:
            choices.append((voice, f"{voice} (not on the agent)"))
        return f'<select name="carrier.{i}.voice" aria-label="Voice">' + "".join(
            f'<option value="{escape(c)}"{" selected" if c == voice else ""}>{escape(label)}</option>'
            for c, label in choices) + "</select>"

    cells = "".join(
        f'<tr><td><input class="unit" name="carrier.{i}.unit" value="{escape(unit)}" placeholder="e.g. CVN-75 Truman" '
        f'aria-label="Carrier unit name"></td><td><input name="carrier.{i}.frequency_mhz" type="number" step="any" '
        f'value="{escape(mhz)}" placeholder="MHz" aria-label="Frequency (MHz)"></td>'
        f'<td><select name="carrier.{i}.modulation" aria-label="Modulation">' + "".join(
            f'<option{" selected" if m == mod else ""}>{m}</option>' for m, _ in ac.MODULATIONS) + "</select></td>"
        f"<td>{voice_cell(i, voice)}</td></tr>"
        for i, (unit, mhz, mod, voice) in enumerate(rows))
    return ('<p style="margin:12px 0 4px"><strong>Carriers</strong></p><p class="sub" style="margin:0">'
            "By the carrier's unit name in the mission. A carrier listed with a frequency uses it; one without uses "
            "the frequency set for it in the mission, else the default above. A carrier with a voice speaks with it, "
            "whatever the voice above says. Clear a name to remove it.</p>"
            f'<input type="hidden" name="carriers" value="{len(rows)}">'
            '<div class="scroll carriers"><table class="results"><thead><tr><th>Carrier</th><th>MHz</th><th>Modulation'
            f"</th><th>Voice</th></tr></thead><tbody>{cells}</tbody></table></div>")


def _setting_row(key: str, label: str, control: str, hint: str = "", title: str = "") -> str:
    """A settings row: its name, the control (`<select name=…>` or `<input name=…>`), and the line under it. Only the
    control itself takes a click (user: not the row, nor the name): the name is plain text, tied to the control
    for screen readers by aria-labelledby."""
    ident = f"setting-{key}"
    control = control.replace(f' name="{key}"', f' id="{escape(ident)}" aria-labelledby="{escape(ident)}-name" '
                                                 f'name="{key}"', 1)
    tip = f' title="{escape(title)}"' if title else ""
    return f'<div class="setting"><span class="name" id="{escape(ident)}-name"{tip}>{label}</span>{control}{hint}</div>'


def _access(members_only: bool, approval: bool) -> str:
    """Who can see the board, and whether new pilots wait for an admin: hub settings kept apart from
    hub/settings.py (`members_only`, `join_approval` on the Save form; app.admin_settings)."""
    def row(name: str, label: str, choices: tuple[tuple[str, str, str], ...], value: str) -> str:
        """`choices`: value, label, and the hint for it (shown for the chosen one, following the dropdown)."""
        options = "".join(f'<option value="{v}" data-hint="{escape(hint)}"{" selected" if v == value else ""}>'
                          f"{escape(text)}</option>" for v, text, hint in choices)
        hint = next(h for v, _, h in choices if v == value)
        return _setting_row(name, label, f'<select name="{name}">{options}</select>',
                            f'<span class="hint">{escape(hint)}</span>')

    see = row("members_only", "Who can see the board",
              (("0", "Anyone", "Viewable by anyone"), ("1", "Members only", "Users must sign in to view anything")),
              "1" if members_only else "0")
    approve = row("join_approval", "Approving new pilots",
                  (("0", "Off", "New users can join without approval"), ("1", "On", "New users require admin approval")),
                  "1" if approval else "0")
    return '<h3 style="margin-top:0">Access</h3>' + see + approve


# The hub settings on /admin (hub/settings.py), in groups.
HUB_SETTING_GROUPS = (("Who can send landings", ("pilot_hook_accept", "require_upload_token")),
                      ("The board", ("other_servers",)),
                      ("Discord", ("discord_board_webhook", "discord_traps_webhook", "public_url")),
                      ("Voice", ("default_voice",)))


def _hub_settings(settings: dict, voices: list, access: str = "") -> str:
    """The hub settings admins change here, one form saved at once: `access` (see `_access`), then the rest
    (hub/settings.py)."""
    from .settings import BY_KEY

    def field(key: str) -> str:
        h, value = BY_KEY[key], settings.get(key, BY_KEY[key].default)
        choices = list(h.choices)
        if key == "default_voice" and voices:
            choices = [("", "The first by name")] + [(v, v) for v in voices]
            if value not in [c for c, _ in choices]:
                choices.append((value, f"{value} (not on this hub)"))
        if choices:  # each option carries what it means, for the line under it (UNSAVED_SCRIPT)
            control = f'<select name="{key}">' + "".join(
                f'<option value="{escape(c)}" data-hint="{escape(h.said(c))}"{" selected" if c == value else ""}>'
                f"{escape(label)}</option>" for c, label in choices) + "</select>"
        else:
            kind = ' type="url" autocomplete="off" spellcheck="false"' if h.url else ""
            control = (f'<input name="{key}"{kind} value="{escape(value)}" maxlength="500" '
                       f'data-hint-empty="{escape(h.said(""))}" data-hint-set="{escape(dict(h.says).get("*", ""))}">')
        return _setting_row(key, escape(h.label), control, f'<span class="hint">{escape(h.said(value))}</span>',
                            title=h.help)

    groups = "".join(f'<h3>{escape(title)}</h3>' + "".join(field(k) for k in keys) for title, keys in HUB_SETTING_GROUPS)
    return ('<div class="section-header"><h2 class="section">Hub settings</h2></div>'
            '<div class="settings panel" style="margin-bottom:12px">'
            f'<form method="post" action="/admin/settings" data-unsaved>{access}{groups}'
            '<p><button type="submit">Save hub settings</button></p></form></div>'
            f"<script>{UNSAVED_SCRIPT}</script>")


# A star by each setting changed since the page loaded (back to as it was: no star), until it's saved.
UNSAVED_SCRIPT = """
document.querySelectorAll('form[data-unsaved]').forEach((form) => {
  const fields = [...form.querySelectorAll('.setting')].map((label) => {
    const input = label.querySelector('select, input, textarea');
    if (!input) return null;
    const value = () => input.type === 'checkbox' ? String(input.checked) : input.value;
    return {label, input, value, saved: value()};
  }).filter(Boolean);
  const mark = () => fields.forEach((f) => {
    f.label.classList.toggle('unsaved', f.value() !== f.saved);
    // The line under it says what the value means: the chosen option's, or a text field's when empty or set.
    const d = f.input.dataset, v = f.input.value.trim(), option = f.input.selectedOptions && f.input.selectedOptions[0];
    const hint = option ? option.dataset.hint
      : d.hintSet !== undefined ? (v ? d.hintSet.split('{value}').join(v) : d.hintEmpty) : undefined;
    if (hint !== undefined) f.label.querySelector('.hint').textContent = hint;
  });
  form.addEventListener('input', mark);
  form.addEventListener('change', mark);
});
"""


def _js_text(text: str) -> str:
    """Text for inside a single-quoted JavaScript string in an HTML attribute (e.g. a confirm() prompt)."""
    js = text.replace("\\", "\\\\").replace("'", "\\'")
    return escape(js, quote=False).replace('"', "&quot;")


def _waiting(pending: list, approval: bool, post) -> str:
    """Pilots waiting for an admin (hub.service.PendingPilot), with Approve and Refuse; nothing if none are."""
    if not pending:
        return ""
    rows = "".join(
        f'<tr><td><a href="/pilots/{quote(p.name, safe="")}">{escape(p.name)}</a></td><td>{escape(_when(p.since))}</td>'
        f"<td>{p.passes}</td><td><div class=\"actions\">"
        + post(f"/admin/pilots/{quote(p.name, safe='')}/approve", "Approve")
        + post(f"/admin/pilots/{quote(p.name, safe='')}/refuse", "Refuse",
               "Refuse this pilot? The name is free again: removed if it has no passes, else back to unclaimed.",
               danger=True)
        + "</div></td></tr>" for p in pending)
    after = '<p class="sub">Turning approval off doesn\'t approve those waiting.</p>' if approval else ""
    return ('<div class="panel" style="margin-bottom:12px"><h2 style="margin-top:0">Waiting for approval</h2>'
            '<div class="scroll"><table class="results"><thead><tr><th>Pilot</th><th>Asked</th><th>Passes</th>'
            f"<th></th></tr></thead><tbody>{rows}</tbody></table></div>{after}</div>")


_OPEN_SENT = """onclick="document.getElementById('whats-sent').showModal()\""""


def _telemetry_notice(tel: dict) -> str:
    """Until an admin has seen it: that usage reports are on, with what's sent and how to turn them off."""
    if not tel or not tel.get("enabled") or tel.get("acknowledged"):
        return ""
    return ('<div class="notice admin"><strong>This hub sends anonymous usage counts to the burble project daily'
            '</strong> · no names, IP addresses, or tracks '
            f'<button type="button" class="link" {_OPEN_SENT}>What\'s sent</button><div class="actions">'
            '<form method="post" action="/admin/telemetry"><input type="hidden" name="on" value="1">'
            '<button type="submit">Keep sending</button></form>'
            '<form method="post" action="/admin/telemetry"><input type="hidden" name="on" value="0">'
            '<button type="submit">Turn off</button></form></div></div>')


# Tables marked data-sortable: a click on a column heading (th[data-sort], "text" or "num") sorts the rows by its
# cells' data-value, and again the other way. Numbers start biggest first (most passes, newest landing).
SORT_SCRIPT = """
document.querySelectorAll('table[data-sortable]').forEach((table) => {
  const heads = [...table.tHead.rows[0].cells];
  heads.forEach((th, col) => {
    if (!th.dataset.sort) return;
    th.querySelector('button').addEventListener('click', () => {
      const num = th.dataset.sort === 'num';
      const was = th.getAttribute('aria-sort');
      const up = was ? was === 'descending' : !num;
      heads.forEach((h) => h.removeAttribute('aria-sort'));
      th.setAttribute('aria-sort', up ? 'ascending' : 'descending');
      const key = (row) => row.cells[col].dataset.value;
      const rows = [...table.tBodies[0].rows].sort((a, b) => {
        const x = key(a), y = key(b);
        const c = num ? Number(x) - Number(y) : x.localeCompare(y);
        return up ? c : -c;
      });
      table.tBodies[0].append(...rows);
    });
  });
});
"""


def _help_icon(text: str) -> str:
    """A small "?" that shows `text` in a themed popup on hover (and on focus by keyboard, and to screen readers)."""
    return (f'<span class="help-icon pop-host" tabindex="0" role="img" aria-label="{escape(text)}">?'
            f'<span class="pop text left" aria-hidden="true">{escape(text)}</span></span>')


def _telemetry_panel(tel: dict) -> str:
    """Usage reports: on/off, where they go (and how each last went), the report as sent now, and what is and isn't
    in it; and connecting a receiver for the Usage page."""
    from .telemetry import NEVER_SENT, WHAT_IS_SENT

    on = tel["enabled"]
    toggle = ('<form method="post" action="/admin/telemetry">'
              f'<input type="hidden" name="on" value="{"0" if on else "1"}">'
              f'<button type="submit"{" class=danger" if on else ""}>{"Turn off" if on else "Turn on"}</button></form>')
    rows = []
    for url in tel["urls"]:
        st = tel["status"].get(url)
        at = _when(datetime.fromisoformat(st["at"])) if st else ""
        when = (f"sent {at}" if st["ok"] else f"failed {at}: {st['message']}") if st else "not sent yet"
        rows.append(f"<li><code>{escape(url)}</code> <span class=\"sub\">({escape(when)})</span></li>")
    where = f"<ul>{''.join(rows)}</ul>" if rows else '<p class="sub">No addresses, so nothing is sent</p>'
    is_default = tel["urls"] == tel["default"]
    urls_form = ('<details><summary>Change where reports go</summary>'
                 '<form method="post" action="/admin/telemetry/urls">'
                 f'<textarea name="urls" spellcheck="false" style="min-height:4em">{escape(chr(10).join(tel["urls"]))}'
                 '</textarea><p class="sub">One receiver per line (<code>burble telemetry serve</code>)</p>'
                 '<div class="actions"><button type="submit">Save addresses</button>'
                 + ("" if is_default else '<button type="submit" name="default" value="1">Back to the project\'s</button>')
                 + "</div></form></details>")
    view = tel.get("view")
    view_form = (f'<p>Showing stats from <code>{escape(view)}</code> on the <a href="/admin/usage">Usage page</a>.</p>'
                 '<form method="post" action="/admin/telemetry/view"><button type="submit" name="clear" value="1">'
                 "Disconnect</button></form>" if view else
                 '<p class="sub">Connect a receiver to see its stats here.</p>'
                 '<form method="post" action="/admin/telemetry/view" class="actions">'
                 '<input name="url" required placeholder="https://telemetry.example.com" style="width:18em">'
                 '<input name="key" type="password" required placeholder="API key or password" autocomplete="off">'
                 '<button type="submit">Connect</button>'
                 + _help_icon("For an API key, run burble telemetry new-key NAME on the receiver's machine. "
                              "Its password works too.")
                 + "</form>")
    sent = "".join(f"<li>{escape(x)}</li>" for x in WHAT_IS_SENT)
    never = "".join(f"<li>{escape(x)}</li>" for x in NEVER_SENT)
    state = "<strong>On</strong>: sent once a day" if on else "<strong>Off</strong>: nothing is sent"
    return ('<div class="panel" id="usage-reports" style="margin-top:12px"><h2 style="margin-top:0">Usage reports</h2>'
            f'<p>{state}</p>'
            f'<div class="actions">{toggle}<button type="button" {_OPEN_SENT}>What\'s sent</button></div>'
            '<dialog class="modal" id="whats-sent"><h2 style="margin-top:0">What\'s sent</h2>'
            f"<p class=\"sub\" style=\"margin:0\">Covers the last 30 days</p><ul>{sent}</ul>"
            f"<h3>What's never sent</h3><ul>{never}</ul>"
            f'<details><summary>The report as it would be sent now</summary><pre>{escape(tel["report"])}</pre></details>'
            '<form method="dialog" class="actions" style="margin-top:8px"><button>Close</button></form></dialog>'
            f"<h3>Where it goes</h3>{where}{urls_form}"
            f"<h3>Usage page</h3>{view_form}</div>")


def usage_page(stats: dict | None, view: str | None, error: str | None) -> str:
    """Admins' Usage page: the stats of the receiver they connected (fetched from its /api/v1/usage)."""
    if view is None:
        content = ('<div class="panel"><p>No usage receiver connected. Connect one under "Usage reports" on the '
                   '<a href="/admin#usage-reports">admin page</a>.</p></div>')
    elif error:
        content = (f'<div class="panel"><p class="error">{escape(error)}</p><p>Check the address and key under '
                   '"Usage reports" on the <a href="/admin#usage-reports">admin page</a>.</p></div>')
    else:
        content = f'<div class="panel">{usage_body(stats)}</div>'
    source = f'<p class="sub">From <code>{escape(view)}</code> · <a href="/admin">Admin</a></p>' if view else ""
    return _page("Usage", f"<style>{USAGE_STYLE}</style>"
                 + _header("Usage.") + f"{source}{content}", "admin")


def _appearance(css: str) -> str:
    """The admins' custom CSS box, with the theme variables it can set."""
    names = ", ".join(f"<code>{v}</code>" for v in [*COLOR_VARS.values(), FONT_VAR, *GRADE_VARS.values()])
    icon = CURRENT_ICON.get()
    preview = f'<img src="/icon?v={escape(icon)}" alt="The current icon">' if icon else "GB"
    remove = ('<form method="post" action="/admin/icon/remove"><button type="submit" class="button paper">Use GB</button>'
              "</form>" if icon else "")
    icon_row = ('<h3>Icon' + _help_icon("Shown in the square at the top left of every page and as the browser tab's "
                                        "icon. A square PNG, JPEG, GIF, WebP, or ICO image, at most 512 KB.")
                + f'</h3><div class="icon-row"><span class="preview">{preview}</span>'
                '<form method="post" action="/admin/icon" enctype="multipart/form-data">'
                '<input type="file" name="icon" required accept="image/png,image/jpeg,image/gif,image/webp,image/x-icon,.ico" '
                'aria-label="Icon image"><button type="submit" class="button dark">Save icon</button></form>' + remove + "</div>")
    return ('<div class="panel" style="margin-top:12px"><h2 style="margin-top:0">Appearance</h2>' + icon_row +
            '<h3>CSS</h3><p class="sub">Custom CSS for every page</p>'
            f'<p class="sub">Discord images only pick up these variables from <code>:root</code> (dark-mode values '
            f"first): {names}</p>"
            '<form method="post" action="/admin/appearance">'
            f'<textarea name="css" spellcheck="false" maxlength="{MAX_CSS}" '
            'placeholder=":root { --bg: #101820; --card: #18242f; --grade-ok: #00c853; }">'
            f'{escape(css)}</textarea><button type="submit">Save CSS</button></form></div>')


def not_admin_page(pilot: str | None) -> str:
    """/admin for someone who isn't an admin (or isn't signed in)."""
    if pilot:
        text = (f"You're signed in as <strong>{escape(pilot)}</strong>, who isn't an admin on this board. "
                "If you should be, ask one of its admins to give you the role.")
        action = f'<a href="/pilots/{quote(pilot, safe="")}">Your passes</a> · <a href="/">Greenie board</a>'
    else:
        text = "This page is for the board's admins. Sign in as a pilot who's an admin to use it."
        action = f'<a href="/signin?next={quote("/admin", safe="")}">Sign in</a> · <a href="/">Greenie board</a>'
    body = _header("Admins only.") + f'<div class="panel"><p>{text}</p><p>{action}</p></div>'
    return _page("Admins only", body)
