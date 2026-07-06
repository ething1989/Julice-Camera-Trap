# BirdNET Global Species Packs

Generated UTC: 2026-05-30T20:49:52+00:00
Grid size: 5.0 degrees
Week: -1 (-1 = year-round)
Threshold: 0.03

## Folder layout
- `cells/`: one `.txt` species file per lat/lon cell center.
- `regions/`: broad region species files and count tables.
- `metadata/cell_index.csv|json`: map cells to coordinate ranges and file names.
- `metadata/region_index.csv|json`: map regions to coordinate ranges and files.
- `metadata/build_summary.json`: build stats.

## Cell file naming
`cell_<latTag>_<lonTag>_g<grid>deg.txt`
- Example: `cell_S03p50_W064p50_g5deg.txt`

## Notes
- Species labels are BirdNET labels (`ScientificName_CommonName`).
- Region files are union lists sorted by how many cells include each species.
- No seasonal filtering unless `week` is changed from `-1`.