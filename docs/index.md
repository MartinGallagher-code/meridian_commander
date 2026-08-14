# Meridian Commander

*The meridian is noon — the other end of the clock from midnight.*

A two-pane terminal file manager with the keys of **Midnight Commander** and
the face of **Turbo Vision**, written in pure Python. It browses local **and
networked** locations (SFTP/SSH/FTP), copies and moves files between the two
panes regardless of where each side lives, synchronizes directories so both
panes hold the newest version of every file, and ships with a built-in file
viewer and editor — plus browsers for `.xlsx` grids, `.docx` documents,
`.pptx` slides, rendered Markdown, colour images, PDFs, and zip/tar archives
navigable as directories.

![The main screen: two panes in Turbo Vision colours](assets/main.png)

## Quick start

```bash
pip install "meridian-commander[ssh]"
meridian
```

With no arguments the left pane opens in the current directory and the right
pane in your home directory. Press **F1** for help, **F2** to connect a pane
to a remote location, **Tab** to switch panes, and **F10** to quit.

## Where things are

- **[Website](https://martingallagher-code.github.io/meridian_commander/)** —
  screenshots and a tour.
- **[README on GitHub](https://github.com/MartinGallagher-code/meridian_commander#readme)**
  — the complete feature guide and key-binding reference.
- **[PyPI](https://pypi.org/project/meridian-commander/)** — releases.

## Manual

```{toctree}
:maxdepth: 2

installation
DATA_TOOLS
plugins
api
```
