# Paper intake sample

`list_photo.truth.json` is the ground-truth extraction a vision model would return for a photo of a handwritten
client list (7 rows, columns name / phone / expiry / consent). It is the FAKE answer for `intake` in FAKE mode.
It contains: a messy phone (row 3, letter O, low confidence, second read disagrees), a second low-confidence phone
(row 7), a blank consent cell (row 4), a tick mark instead of "ΝΑΙ" (row 5), and a "no" (row 6).
Cell shape: `{col, page, row, text, confidence, bbox:[x,y,w,h] as page fractions, mark?, blank?, second_read?, flag?}`.

The real photo `list_photo.jpg` is NOT included. The reviewer takes it: print or hand-write these rows on paper,
photograph it, save it here as `list_photo.jpg`, and put its sha256 into the truth file. All numbers are fake.
