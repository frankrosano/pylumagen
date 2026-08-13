---
inclusion: manual
---

# References — Lumagen Documentation

Primary-source Lumagen docs and third-party drivers live in the **sibling repo** `lumagen-research` (`../lumagen-research`). That repo is **private** — the material is largely under Lumagen, Inc.'s copyright — but it is properly version-controlled. It is the canonical reference for everything `aiolumagen` parses or emits.

It was previously a gitignored `References/` folder inside `esphome-lumagen`, so any `References/x` path in older notes, docstrings or commit messages now means `../lumagen-research/x`.

Pull this in (`#references`) when working on `protocol.py`, `commands.py`, `state.py`, or anywhere you need to verify a wire-format detail.

## What's There

Located at `../lumagen-research/` from this repo:

| File | What it is |
|---|---|
| `Tip0011_RS232CommandInterface_111023.pdf` | **The** Lumagen RS-232 command reference. Authoritative for every `ZQ` query, command syntax, `!`-prefixed response, and report code under "Full v4" mode. The 11/2023 edition predates the firmware's "Full v5" / `!I25` mode (deduced empirically — see `protocol.py` for the v5 layout). |
| `SERIAL_COMMAND_REFERENCE.md` | Readable extraction of the Tip0011 command set. Faster to consult than the PDF, which needs conversion. |
| `Radiance_Pro_Manual_070621.pdf` | User manual. Useful for vocabulary (aspect modes, memories, HDR pipeline) so enum names and docstrings match Lumagen's terms. |
| `FIRMWARE_REVERSE_ENGINEERING_FINDINGS.md` | Firmware-strings RE notes. Source for the undocumented commands this library does implement — the `ZY552X` fan-speed setter among them. **Partly wrong on the firmware wire format** (it claims Motorola S-records); see `FIRMWARE_UPDATE_PROTOCOL.md` Part V.A. |
| `crestron-driver/` | Crestron sample modules (mostly binary). Behavioral oracle for cross-checking command strings and parsing. |
| `Pronto/Lumagen_Pronto_Codes.db` | Pronto IR code database. Discrete-IR equivalents of OSD/aspect/memory commands. |
| `FIRMWARE_UPDATE_PROTOCOL.md` + `lumagen_stage.py` etc. | The firmware-update (bootloader) protocol and its working flasher. **Not implemented in `aiolumagen` today.** Part III.4 of that doc sketches the port into `aiolumagen/bootloader/`; until then this library speaks only the normal control protocol. |
| `radiance_pro<MMDDYY>/` | Vendor updater EXEs, one per release. Source of the firmware images. Not consumed by `aiolumagen`. |

## Rules

- **The protocol of record is `Tip0011_RS232CommandInterface_111023.pdf`.** When third-party drivers and the PDF disagree, trust the PDF and document the discrepancy in code with a comment.
- **Cite Tip0011 sections in code comments** where a parser decision depends on it (`# Tip0011 §3.4: ZQS01 returns...`). Do not paste long verbatim quotes.
- **`lumagen-research` is private and must stay that way.** This repo is public — never copy a PDF, firmware blob, vendor EXE or capture into it. Reference by filename only, which is what the docstrings in `commands.py` and `client.py` do.
- **PDFs aren't directly readable** by the agent. If a passage is needed, ask for a `pdftotext` extract and paste it into the conversation.
- The richer references doc, with file-by-file detail on the firmware tooling, lives in the sibling repo at `../esphome-lumagen/.kiro/steering/references.md`.
