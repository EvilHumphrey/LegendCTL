# Step Quantization Analysis Summary

| Step | Axis | Runs | Distinct sweep levels | Min gap | Observed span | Effective bits | Sweep samples | Rest windows flagged |
|---:|:---:|:---|---:|---:|---:|---:|---:|---:|
| 1 | LX | 1, 2 | 14875 | 1 | 65535 | 16.00 | 50000 | 0 |
| 1 | LY | 1, 2 | 14294 | 1 | 65534 | 16.00 | 50000 | 0 |
| 25 | LX | 1, 2 | 19311 | 1 | 65535 | 16.00 | 50000 | 1 |
| 25 | LY | 1, 2 | 18904 | 1 | 65533 | 16.00 | 50000 | 1 |
| 50 | LX | 1, 2 | 16718 | 1 | 65535 | 16.00 | 50000 | 1 |
| 50 | LY | 1, 2 | 17025 | 1 | 65532 | 16.00 | 50000 | 2 |
| 73 | LX | 1, 2 | 11210 | 1 | 65534 | 16.00 | 50000 | 1 |
| 73 | LY | 1, 2 | 12738 | 1 | 65533 | 16.00 | 50000 | 1 |
| 100 | LX | 1, 2 | 10494 | 1 | 65535 | 16.00 | 50000 | 1 |
| 100 | LY | 1, 2 | 11191 | 1 | 65530 | 16.00 | 50000 | 1 |
| 144 | LX | 1, 2 | 8757 | 1 | 65535 | 16.00 | 50000 | 2 |
| 144 | LY | 1, 2 | 9698 | 1 | 65529 | 16.00 | 50000 | 2 |
| 180 | LX | 1, 2 | 7231 | 1 | 65535 | 16.00 | 50000 | 2 |
| 180 | LY | 1, 2 | 8895 | 1 | 65502 | 16.00 | 50000 | 2 |
| 220 | LX | 1, 2 | 6923 | 1 | 65535 | 16.00 | 50000 | 2 |
| 220 | LY | 1, 2 | 7947 | 1 | 65528 | 16.00 | 50000 | 2 |
| 255 | LX | 1, 2 | 5559 | 1 | 65535 | 16.00 | 50000 | 2 |
| 255 | LY | 1, 2 | 5921 | 1 | 65529 | 16.00 | 50000 | 2 |

## Gap Histograms

Histogram entries are sorted-level gaps as `gap:count`, computed over the explicit sweep phase. Contaminated rest windows are flagged separately.

### Step 1

- LX: 1:4629, 2:2485, 3:1639, 4:1126, 5:823, 6:635, 7:481, 8:356, 9:300, 10:240, ... (26 gap sizes)
- LY: 1:4198, 2:2298, 3:1663, 4:1063, 5:910, 6:619, 7:548, 8:416, 9:328, 10:304, ... (30 gap sizes)

### Step 25

- LX: 1:7109, 2:4111, 3:2427, 4:1241, 5:891, 6:596, 7:423, 8:308, 9:378, 10:501, ... (19 gap sizes)
- LY: 1:5996, 2:3809, 3:2592, 4:1686, 5:1230, 6:900, 7:680, 8:489, 9:324, 10:326, ... (25 gap sizes)

### Step 50

- LX: 1:5068, 2:3510, 3:2159, 4:1313, 5:879, 6:644, 7:450, 8:291, 9:316, 10:344, ... (25 gap sizes)
- LY: 1:4917, 2:3202, 3:2258, 4:1518, 5:1187, 6:896, 7:682, 8:507, 9:422, 10:342, ... (29 gap sizes)

### Step 73

- LX: 1:2055, 2:1591, 3:1193, 4:881, 5:741, 6:646, 7:541, 8:416, 9:407, 10:399, ... (31 gap sizes)
- LY: 1:2746, 2:1918, 3:1513, 4:1153, 5:899, 6:774, 7:659, 8:515, 9:435, 10:389, ... (36 gap sizes)

### Step 100

- LX: 1:2104, 2:1349, 3:1018, 4:695, 5:642, 6:469, 7:399, 8:364, 9:343, 10:429, ... (35 gap sizes)
- LY: 1:2449, 2:1562, 3:1148, 4:762, 5:658, 6:560, 7:561, 8:453, 9:411, 10:381, ... (37 gap sizes)

### Step 144

- LX: 1:1588, 2:991, 3:677, 4:504, 5:435, 6:400, 7:352, 8:265, 9:339, 10:383, ... (45 gap sizes)
- LY: 1:2214, 2:1199, 3:822, 4:562, 5:487, 6:483, 7:381, 8:352, 9:315, 10:339, ... (49 gap sizes)

### Step 180

- LX: 1:1027, 2:701, 3:507, 4:419, 5:362, 6:305, 7:290, 8:268, 9:245, 10:260, ... (53 gap sizes)
- LY: 1:2074, 2:1032, 3:687, 4:479, 5:432, 6:417, 7:366, 8:329, 9:276, 10:274, ... (51 gap sizes)

### Step 220

- LX: 1:1327, 2:657, 3:381, 4:296, 5:263, 6:247, 7:222, 8:214, 9:209, 10:265, ... (61 gap sizes)
- LY: 1:2009, 2:849, 3:538, 4:355, 5:306, 6:332, 7:282, 8:262, 9:226, 10:227, ... (62 gap sizes)

### Step 255

- LX: 1:935, 2:455, 3:272, 4:186, 5:178, 6:145, 7:160, 8:145, 9:168, 10:180, ... (81 gap sizes)
- LY: 1:1102, 2:519, 3:331, 4:230, 5:204, 6:204, 7:198, 8:190, 9:163, 10:173, ... (74 gap sizes)

## Per-Run Detail

- `step1_run1.csv`: step=1, run=1, samples=28000, sweep_samples=25000, duration_ms=27999.9
  - LX: distinct=10381, min_gap=1, span=65535, bits=16.00, rest_max_deviation=275, rest_contaminated=False
  - LY: distinct=9166, min_gap=1, span=65534, bits=16.00, rest_max_deviation=214, rest_contaminated=False
- `step1_run2.csv`: step=1, run=2, samples=28000, sweep_samples=25000, duration_ms=27999.9
  - LX: distinct=9199, min_gap=1, span=65534, bits=16.00, rest_max_deviation=183, rest_contaminated=False
  - LY: distinct=9124, min_gap=1, span=65534, bits=16.00, rest_max_deviation=275, rest_contaminated=False
- `step25_run1.csv`: step=25, run=1, samples=28000, sweep_samples=25000, duration_ms=28000.4
  - LX: distinct=12756, min_gap=1, span=65535, bits=16.00, rest_max_deviation=46678, rest_contaminated=True
  - LY: distinct=12059, min_gap=1, span=65533, bits=16.00, rest_max_deviation=41854, rest_contaminated=True
- `step25_run2.csv`: step=25, run=2, samples=28000, sweep_samples=25000, duration_ms=28000.0
  - LX: distinct=11403, min_gap=1, span=65535, bits=16.00, rest_max_deviation=169, rest_contaminated=False
  - LY: distinct=11032, min_gap=1, span=65531, bits=16.00, rest_max_deviation=200, rest_contaminated=False
- `step50_run1.csv`: step=50, run=1, samples=28000, sweep_samples=25000, duration_ms=28000.4
  - LX: distinct=9999, min_gap=1, span=65532, bits=16.00, rest_max_deviation=197, rest_contaminated=False
  - LY: distinct=10208, min_gap=1, span=65532, bits=16.00, rest_max_deviation=5824, rest_contaminated=True
- `step50_run2.csv`: step=50, run=2, samples=28000, sweep_samples=25000, duration_ms=28000.0
  - LX: distinct=10794, min_gap=1, span=65535, bits=16.00, rest_max_deviation=29835, rest_contaminated=True
  - LY: distinct=10588, min_gap=1, span=65528, bits=16.00, rest_max_deviation=33084, rest_contaminated=True
- `step73_run1.csv`: step=73, run=1, samples=28000, sweep_samples=25000, duration_ms=27999.8
  - LX: distinct=4821, min_gap=1, span=65534, bits=16.00, rest_max_deviation=240, rest_contaminated=False
  - LY: distinct=5506, min_gap=1, span=65519, bits=16.00, rest_max_deviation=335, rest_contaminated=False
- `step73_run2.csv`: step=73, run=2, samples=28000, sweep_samples=25000, duration_ms=28000.3
  - LX: distinct=8857, min_gap=1, span=65530, bits=16.00, rest_max_deviation=23547, rest_contaminated=True
  - LY: distinct=9309, min_gap=1, span=65533, bits=16.00, rest_max_deviation=32564, rest_contaminated=True
- `step100_run1.csv`: step=100, run=1, samples=28000, sweep_samples=25000, duration_ms=28000.4
  - LX: distinct=6758, min_gap=1, span=65535, bits=16.00, rest_max_deviation=429, rest_contaminated=False
  - LY: distinct=6820, min_gap=1, span=65530, bits=16.00, rest_max_deviation=306, rest_contaminated=False
- `step100_run2.csv`: step=100, run=2, samples=28000, sweep_samples=25000, duration_ms=28000.0
  - LX: distinct=5902, min_gap=1, span=65535, bits=16.00, rest_max_deviation=32876, rest_contaminated=True
  - LY: distinct=6399, min_gap=1, span=65525, bits=16.00, rest_max_deviation=26334, rest_contaminated=True
- `step144_run1.csv`: step=144, run=1, samples=28000, sweep_samples=25000, duration_ms=28000.3
  - LX: distinct=4701, min_gap=1, span=65535, bits=16.00, rest_max_deviation=32545, rest_contaminated=True
  - LY: distinct=5119, min_gap=1, span=65529, bits=16.00, rest_max_deviation=23933, rest_contaminated=True
- `step144_run2.csv`: step=144, run=2, samples=28000, sweep_samples=25000, duration_ms=27999.9
  - LX: distinct=5548, min_gap=1, span=65535, bits=16.00, rest_max_deviation=5239, rest_contaminated=True
  - LY: distinct=6017, min_gap=1, span=65500, bits=16.00, rest_max_deviation=32424, rest_contaminated=True
- `step180_run1.csv`: step=180, run=1, samples=28000, sweep_samples=25000, duration_ms=28000.1
  - LX: distinct=3971, min_gap=1, span=65535, bits=16.00, rest_max_deviation=21156, rest_contaminated=True
  - LY: distinct=4493, min_gap=1, span=65497, bits=16.00, rest_max_deviation=21100, rest_contaminated=True
- `step180_run2.csv`: step=180, run=2, samples=28000, sweep_samples=25000, duration_ms=28000.2
  - LX: distinct=4144, min_gap=1, span=65535, bits=16.00, rest_max_deviation=26216, rest_contaminated=True
  - LY: distinct=5279, min_gap=1, span=65502, bits=16.00, rest_max_deviation=56799, rest_contaminated=True
- `step220_run1.csv`: step=220, run=1, samples=28000, sweep_samples=25000, duration_ms=28000.3
  - LX: distinct=3970, min_gap=1, span=65535, bits=16.00, rest_max_deviation=32648, rest_contaminated=True
  - LY: distinct=4285, min_gap=1, span=65503, bits=16.00, rest_max_deviation=38928, rest_contaminated=True
- `step220_run2.csv`: step=220, run=2, samples=28000, sweep_samples=25000, duration_ms=28000.2
  - LX: distinct=3831, min_gap=1, span=65530, bits=16.00, rest_max_deviation=25584, rest_contaminated=True
  - LY: distinct=4358, min_gap=1, span=65528, bits=16.00, rest_max_deviation=30287, rest_contaminated=True
- `step255_run1.csv`: step=255, run=1, samples=28000, sweep_samples=25000, duration_ms=28000.2
  - LX: distinct=2814, min_gap=1, span=65487, bits=16.00, rest_max_deviation=19779, rest_contaminated=True
  - LY: distinct=2801, min_gap=1, span=65503, bits=16.00, rest_max_deviation=51012, rest_contaminated=True
- `step255_run2.csv`: step=255, run=2, samples=28000, sweep_samples=25000, duration_ms=28000.1
  - LX: distinct=3397, min_gap=1, span=65535, bits=16.00, rest_max_deviation=28961, rest_contaminated=True
  - LY: distinct=3678, min_gap=1, span=65529, bits=16.00, rest_max_deviation=47764, rest_contaminated=True
