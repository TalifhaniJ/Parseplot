Parseplot

A specialized Python toolkit for extracting geotechnical data from PDF reports into clean, long-format CSVs. This suite is designed to integrate into the MaplyticX ecosystem for geospatial and geological visualization.
Features
1. Borehole Parser (parse-plot)

Specifically engineered for dotPLOT and hotPLOT formats. It processes PDFs where each page represents a single borehole and generates two distinct long-format outputs:

    Lithology Intervals: Extracts material descriptions, depth boundaries, and water levels.

    Drilling Runs: Captures per-run data including drilling method, RQD, core recovery, and SPT-N values.

2. Test Pit Parser (parse-testpit)

Designed for tabular geotechnical reports. It extracts:

    Lab Results: Processes tables containing grading (GM) and plasticity (LL, PI, LS) data.

    Lithologies: Maps material descriptions to their specific depth intervals.

Installation

This suite requires Python 3.12.3 or higher.
Bash

pip install parseplot

Usage
Parsing Borehole Logs

To parse a dotPLOT PDF into lithology and drilling CSVs:
Bash

parse-borehole report.pdf --out-dir ./output_folder

Parsing Test Pit Tables

To extract lab results and material intervals from a test pit table:
Bash

parse-testpit testpit_report.pdf output.csv

Data Structure

The tool outputs long-format CSVs compatible with standard geological modeling software.
Column	Description
borehole_id	Normalized ID (e.g., BH-01 or TP-01).
depth_top	Starting depth of the interval.
material	Canonical material description with bracketed lab data.
y_position	Latitude (Decimal Degrees).
x_position	Longitude (Decimal Degrees).
Development & Environment

The suite is developed primarily for Linux environments using Python 3.12.3. It relies on pdfplumber for robust coordinate-based text extraction.
Local Setup

    Clone the repository.

    Ensure Python 3.12.3 is active.

    Install dependencies:
    Bash

    pip install pdfplumber