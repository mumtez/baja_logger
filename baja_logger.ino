// baja_logger.ino
//
// ESP32-C3 + MPU6050 + ADXL375 dual-accelerometer logger.
// Streams raw counts over USB serial as CSV. Conversion to g happens
// on the laptop so nothing is lost to rounding here.
//
// Board:  "ESP32C3 Dev Module"  (USB CDC On Boot: Enabled)
// Wiring: both sensors share one I2C bus. Different addresses, no conflict.
//
//   ESP32-C3        MPU6050        ADXL375
//   3V3        ---  VCC        ---  VIN
//   GND        ---  GND        ---  GND
//   GPIO4 SDA  ---  SDA        ---  SDA
//   GPIO5 SCL  ---  SCL        ---  SCL
//
// Serial commands (type in the monitor, press enter):
//   r          start / stop streaming
//   z          reset counters and the sample clock
//   <anything> recorded into the stream as "# note: <text>"

#include <Wire.h>

// ------------------------------------------------------------------ wiring
//
// Pins follow whichever chip this is compiled for, so the SAME sketch flashes
// to both boards. Pick the board in Tools > Board and the right pins come
// with it. Both sets are the wiring confirmed by i2c_scan.
//
// ESP32-C3 (perf board)  : Tools > Board > ESP32C3 Dev Module
//                          plus USB CDC On Boot > Enabled
// ESP32-WROOM (breadboard): Tools > Board > ESP32 Dev Module
//
#if defined(CONFIG_IDF_TARGET_ESP32C3)
  #define CHIP_NAME  "esp32c3"
  #define I2C_SDA    10
  #define I2C_SCL     2        // strapping pin; the I2C pull-up holds it high at boot
#else
  #define CHIP_NAME  "esp32-wroom"
  #define I2C_SDA    21
  #define I2C_SCL    22
#endif

#define I2C_HZ    400000UL      // fast mode; 100000 also works but is tight at 1 kHz

// --------------------------------------------------------------- sampling
#define SAMPLE_HZ      1000UL
#define PERIOD_US      (1000000UL / SAMPLE_HZ)

// ---------------------------------------------------------------- MPU6050
#define MPU_ADDR            0x68   // 0x69 if AD0 is tied high
#define MPU_WHO_AM_I        0x75
#define MPU_SMPLRT_DIV      0x19
#define MPU_CONFIG          0x1A
#define MPU_ACCEL_CONFIG    0x1C
#define MPU_PWR_MGMT_1      0x6B
#define MPU_ACCEL_XOUT_H    0x3B

// AFS_SEL: 0 = +/-2g, 1 = +/-4g, 2 = +/-8g, 3 = +/-16g
// +/-8g leaves headroom for table taps without clipping. Raise to 3 on the car.
#define MPU_AFS_SEL         2

// DLPF_CFG: 0 = 260 Hz, 1 = 184 Hz, 2 = 94 Hz, 3 = 44 Hz, 4 = 21 Hz
// This is the anti-alias filter. At 1 kHz sampling Nyquist is 500 Hz, so 0
// (260 Hz) is the widest setting that still rolls off before the fold point.
#define MPU_DLPF            0

// ---------------------------------------------------------------- ADXL375
// 0 = skip it entirely and emit zeros in its three columns. Set to 1 once it
// shares the pins above. Leaving it on while it is unreachable just burns
// ~200 us per sample on a read that always fails.
#define USE_ADXL            0

#define ADXL_ADDR           0x1D   // 0x53 if SDO/ALT ADDRESS is tied low
#define ADXL_DEVID          0x00
#define ADXL_BW_RATE        0x2C
#define ADXL_POWER_CTL      0x2D
#define ADXL_DATA_FORMAT    0x31
#define ADXL_DATAX0         0x32

// BW_RATE 0x0D = 800 Hz output rate, 400 Hz internal bandwidth.
#define ADXL_RATE           0x0D
#define ADXL_LSB_PER_G      20.5f  // 49 mg per count, fixed +/-200g part

static const float MPU_LSB_PER_G[4] = { 16384.0f, 8192.0f, 4096.0f, 2048.0f };
static const int   MPU_RANGE_G[4]   = { 2, 4, 8, 16 };
static const int   MPU_DLPF_HZ[5]   = { 260, 184, 94, 44, 21 };

// ------------------------------------------------------------ ring buffer
// Decouples sampling from the USB write so a serial hiccup cannot jitter
// the sample clock. 256 samples is a quarter second of slack at 1 kHz.
struct Sample {
  uint32_t t;
  int16_t  a[6];
};

#define RING_N 256
static Sample   ring[RING_N];
static uint16_t r_head = 0, r_tail = 0;

// ----------------------------------------------------------------- state
static bool     running   = false;
static uint32_t n_sent    = 0;   // samples pushed into the ring
static uint32_t n_dropped = 0;   // ring was full, sample thrown away
static uint32_t n_late    = 0;   // loop fell a whole period behind
static uint32_t n_clip    = 0;   // MPU6050 hit the rail on some axis
static uint32_t n_i2cerr  = 0;
static uint32_t t_next    = 0;
static uint32_t t_stats   = 0;
static char     cmd[80];
static uint8_t  cmd_len   = 0;

// ------------------------------------------------------------- i2c helpers
static bool regWrite(uint8_t addr, uint8_t reg, uint8_t val) {
  Wire.beginTransmission(addr);
  Wire.write(reg);
  Wire.write(val);
  return Wire.endTransmission() == 0;
}

static bool regRead(uint8_t addr, uint8_t reg, uint8_t *dst, uint8_t n) {
  Wire.beginTransmission(addr);
  Wire.write(reg);
  if (Wire.endTransmission(false) != 0) return false;
  if (Wire.requestFrom((int)addr, (int)n) != n) return false;
  for (uint8_t i = 0; i < n; i++) dst[i] = Wire.read();
  return true;
}

// --------------------------------------------------------------- sensors
static bool mpuInit() {
  if (!regWrite(MPU_ADDR, MPU_PWR_MGMT_1, 0x80)) return false;  // reset
  delay(100);
  if (!regWrite(MPU_ADDR, MPU_PWR_MGMT_1, 0x01)) return false;  // wake, PLL/X-gyro clock
  delay(10);
  if (!regWrite(MPU_ADDR, MPU_CONFIG, MPU_DLPF)) return false;
  if (!regWrite(MPU_ADDR, MPU_ACCEL_CONFIG, MPU_AFS_SEL << 3)) return false;
  if (!regWrite(MPU_ADDR, MPU_SMPLRT_DIV, 0)) return false;
  return true;
}

static bool adxlInit() {
  if (!regWrite(ADXL_ADDR, ADXL_DATA_FORMAT, 0x0B)) return false;  // full res, right justified
  if (!regWrite(ADXL_ADDR, ADXL_BW_RATE, ADXL_RATE)) return false;
  if (!regWrite(ADXL_ADDR, ADXL_POWER_CTL, 0x08)) return false;    // measure mode
  return true;
}

// MPU6050 is big-endian, ADXL375 is little-endian. Easy thing to get wrong.
static bool mpuRead(int16_t *out) {
  uint8_t b[6];
  if (!regRead(MPU_ADDR, MPU_ACCEL_XOUT_H, b, 6)) return false;
  for (int i = 0; i < 3; i++)
    out[i] = (int16_t)(((uint16_t)b[2 * i] << 8) | b[2 * i + 1]);
  return true;
}

static bool adxlRead(int16_t *out) {
  uint8_t b[6];
  if (!regRead(ADXL_ADDR, ADXL_DATAX0, b, 6)) return false;
  for (int i = 0; i < 3; i++)
    out[i] = (int16_t)(((uint16_t)b[2 * i + 1] << 8) | b[2 * i]);
  return true;
}

// ---------------------------------------------------------------- output
static void header() {
  Serial.println(F("# baja dual-accel logger"));
  Serial.printf("# chip=%s sda=%d scl=%d\n", CHIP_NAME, I2C_SDA, I2C_SCL);
  Serial.printf("# sample_hz=%lu\n", (unsigned long)SAMPLE_HZ);
  Serial.printf("# mpu6050 addr=0x%02X range_g=%d lsb_per_g=%.1f dlpf_hz=%d\n",
                MPU_ADDR, MPU_RANGE_G[MPU_AFS_SEL],
                MPU_LSB_PER_G[MPU_AFS_SEL], MPU_DLPF_HZ[MPU_DLPF]);
#if USE_ADXL
  Serial.printf("# adxl375 addr=0x%02X range_g=200 lsb_per_g=%.1f bw_hz=400\n",
                ADXL_ADDR, ADXL_LSB_PER_G);
#else
  Serial.println(F("# adxl375 disabled -- its three columns are always zero"));
#endif
  Serial.println(F("# units=raw_counts  divide each column by its lsb_per_g"));
  Serial.println(F("# columns: t_us,mpu_ax,mpu_ay,mpu_az,adxl_ax,adxl_ay,adxl_az"));
}

static void stats() {
  Serial.printf("# stats sent=%lu dropped=%lu late=%lu mpu_clip=%lu i2c_err=%lu\n",
                (unsigned long)n_sent, (unsigned long)n_dropped,
                (unsigned long)n_late, (unsigned long)n_clip,
                (unsigned long)n_i2cerr);
}

// ---------------------------------------------------------------- sampling
static void sampleOnce() {
  Sample s;
  s.t = micros();

  if (!mpuRead(&s.a[0]))  { n_i2cerr++; for (int i = 0; i < 3; i++) s.a[i] = 0; }
#if USE_ADXL
  if (!adxlRead(&s.a[3])) { n_i2cerr++; for (int i = 3; i < 6; i++) s.a[i] = 0; }
#else
  s.a[3] = s.a[4] = s.a[5] = 0;
#endif

  for (int i = 0; i < 3; i++)
    if (s.a[i] >= 32760 || s.a[i] <= -32760) { n_clip++; break; }

  uint16_t nh = (uint16_t)((r_head + 1) % RING_N);
  if (nh == r_tail) { n_dropped++; return; }   // reader fell behind
  ring[r_head] = s;
  r_head = nh;
  n_sent++;
}

static void drain() {
  char buf[72];
  for (int guard = 0; guard < 8 && r_tail != r_head; guard++) {
    if (Serial.availableForWrite() < (int)sizeof(buf)) return;
    Sample &s = ring[r_tail];
    int n = snprintf(buf, sizeof(buf), "%lu,%d,%d,%d,%d,%d,%d\n",
                     (unsigned long)s.t,
                     s.a[0], s.a[1], s.a[2], s.a[3], s.a[4], s.a[5]);
    if (n > 0) Serial.write((const uint8_t *)buf, (size_t)n);
    r_tail = (uint16_t)((r_tail + 1) % RING_N);
  }
}

// ---------------------------------------------------------------- commands
static void startRun() {
  running = true;
  t_next  = micros() + PERIOD_US;
  t_stats = millis();
  Serial.println(F("# run start"));
}

static void stopRun() {
  running = false;
  Serial.println(F("# run stop"));
  stats();
}

static void handleLine(char *line) {
  if (line[0] == '\0') return;

  // 'g' and 's' are absolute, 'r' toggles. With two boards running you want
  // the absolute ones -- a toggle sent to a board that is already streaming
  // stops it instead of starting it.
  if ((line[0] == 'g' || line[0] == 'G') && line[1] == '\0') { startRun(); return; }
  if ((line[0] == 's' || line[0] == 'S') && line[1] == '\0') { stopRun();  return; }

  if ((line[0] == 'r' || line[0] == 'R') && line[1] == '\0') {
    if (running) stopRun(); else startRun();
    return;
  }

  if ((line[0] == 'z' || line[0] == 'Z') && line[1] == '\0') {
    n_sent = n_dropped = n_late = n_clip = n_i2cerr = 0;
    r_head = r_tail = 0;
    t_next = micros() + PERIOD_US;
    Serial.println(F("# counters reset"));
    return;
  }

  Serial.printf("# note: %s\n", line);
}

static void pollSerial() {
  while (Serial.available()) {
    char c = (char)Serial.read();
    if (c == '\n' || c == '\r') {
      cmd[cmd_len] = '\0';
      handleLine(cmd);
      cmd_len = 0;
    } else if (cmd_len < sizeof(cmd) - 1) {
      cmd[cmd_len++] = c;
    }
  }
}

// -------------------------------------------------------------------- main
void setup() {
  Serial.begin(921600);          // ignored by native USB CDC, kept for UART bridges
  delay(2000);                   // let the host enumerate before we print

  Wire.begin(I2C_SDA, I2C_SCL, I2C_HZ);

  header();

  uint8_t id = 0;
  if (regRead(MPU_ADDR, MPU_WHO_AM_I, &id, 1))
    Serial.printf("# mpu6050 who_am_i=0x%02X\n", id);
  else
    Serial.println(F("# ERROR mpu6050 not responding -- check wiring and AD0"));

#if USE_ADXL
  if (regRead(ADXL_ADDR, ADXL_DEVID, &id, 1)) {
    Serial.printf("# adxl375 devid=0x%02X%s\n", id,
                  id == 0xE5 ? "" : "  <-- expected 0xE5");
  } else {
    Serial.println(F("# ERROR adxl375 not responding -- check wiring and ALT ADDRESS"));
  }
#endif

  if (!mpuInit())  Serial.println(F("# ERROR mpu6050 init failed"));
#if USE_ADXL
  if (!adxlInit()) Serial.println(F("# ERROR adxl375 init failed"));
#endif

  Serial.println(F("# ready -- send 'r' to start"));
}

void loop() {
  pollSerial();

  if (running) {
    uint32_t now = micros();
    if ((int32_t)(now - t_next) >= 0) {
      t_next += PERIOD_US;
      // If we are more than a few periods behind, give up on catching up
      // and resync. Counted so it shows in the data rather than hiding.
      if ((int32_t)(micros() - t_next) > (int32_t)(PERIOD_US * 4)) {
        t_next = micros() + PERIOD_US;
        n_late++;
      }
      sampleOnce();
    }

    if (millis() - t_stats >= 5000) {
      t_stats = millis();
      stats();
    }
  }

  drain();
}
