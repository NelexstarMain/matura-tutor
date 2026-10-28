#!/usr/bin/env bash
# Zależności raportu tygodniowego (raport.py). Kontener startuje bez LaTeX-a; ok. 1–2 min, ok. 600 MB.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
SUDO=""; [ "$(id -u)" -ne 0 ] && SUDO="sudo"
if ! command -v pdflatex >/dev/null; then
  $SUDO apt-get update -qq
  $SUDO apt-get install -y -qq --no-install-recommends \
    texlive-latex-base texlive-latex-recommended texlive-latex-extra texlive-pictures \
    texlive-lang-polish texlive-fonts-recommended lmodern cm-super poppler-utils >/dev/null
fi
python3 -m pip install -q matplotlib scipy numpy seaborn 2>/dev/null || python3 -m pip install -q --break-system-packages matplotlib scipy numpy seaborn
echo "[instaluj] gotowe: $(pdflatex --version | head -1)"
