// i2c_scan.ino
//
// Finds the sensors. Flash this, open the Serial Monitor at any baud.
//
// It scans every address on the pins configured below. If it finds nothing,
// it sweeps other plausible SDA/SCL pairs looking for the four addresses an
// MPU6050 or ADXL375 can have, so a mis-wired pair shows up by itself.

#include <Wire.h>

// Set these to whatever you believe the wiring is.
#define SDA_PIN 4
#define SCL_PIN 5

// Pins worth trying on an ESP32-C3. 11..17 are the flash, 18/19 are USB,
// 20/21 are UART0. GPIO2, 8 and 9 are strapping pins -- they can work but
// only if nothing is pulling them at boot.
static const uint8_t CAND[] = { 0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10 };
static const uint8_t KNOWN[] = { 0x68, 0x69, 0x53, 0x1D };

static const char *label(uint8_t a) {
  switch (a) {
    case 0x68: return "  MPU6050 (AD0 low)";
    case 0x69: return "  MPU6050 (AD0 high)";
    case 0x53: return "  ADXL375/345 (ALT low)";
    case 0x1D: return "  ADXL375/345 (ALT high)";
    default:   return "";
  }
}

static bool probe(uint8_t addr) {
  Wire.beginTransmission(addr);
  return Wire.endTransmission() == 0;
}

void setup() {
  Serial.begin(921600);
  delay(2500);

  Serial.println();
  Serial.println(F("=== I2C scan ==="));

  Wire.begin(SDA_PIN, SCL_PIN, 100000);
  Wire.setTimeOut(20);
  Serial.printf("Full scan on SDA=%d SCL=%d\n", SDA_PIN, SCL_PIN);

  int found = 0;
  for (uint8_t a = 1; a < 127; a++) {
    if (probe(a)) {
      Serial.printf("  0x%02X%s\n", a, label(a));
      found++;
    }
  }

  if (found) {
    Serial.printf("\n%d device(s) on SDA=%d SCL=%d.\n", found, SDA_PIN, SCL_PIN);
    Serial.println(F("Put those addresses and pins into baja_logger.ino."));
    return;
  }

  Serial.println(F("\nNothing on those pins. Sweeping other pairs..."));
  int hits = 0;
  for (uint8_t i = 0; i < sizeof(CAND); i++) {
    for (uint8_t j = 0; j < sizeof(CAND); j++) {
      if (i == j) continue;
      uint8_t sda = CAND[i], scl = CAND[j];
      if (sda == SDA_PIN && scl == SCL_PIN) continue;

      Wire.end();
      delay(2);
      if (!Wire.begin(sda, scl, 100000)) continue;
      Wire.setTimeOut(20);

      for (uint8_t k = 0; k < sizeof(KNOWN); k++) {
        if (probe(KNOWN[k])) {
          Serial.printf("  SDA=%-2d SCL=%-2d -> 0x%02X%s\n",
                        sda, scl, KNOWN[k], label(KNOWN[k]));
          hits++;
        }
      }
    }
  }

  if (hits) {
    Serial.println(F("\nUse the pins above in baja_logger.ino."));
  } else {
    Serial.println(F("\nNo device answered on any pin pair. That points at"));
    Serial.println(F("power or pull-ups rather than pin assignment:"));
    Serial.println(F("  - VCC on both sensors reading ~3.3 V to GND?"));
    Serial.println(F("  - a common ground between the C3 and the sensors?"));
    Serial.println(F("  - pull-ups on SDA and SCL? Breakouts usually have"));
    Serial.println(F("    them; a bare chip on perf board does not. Add"));
    Serial.println(F("    4.7k from each line to 3V3."));
    Serial.println(F("  - SDA and SCL swapped, or a cold solder joint?"));
  }
}

void loop() {
  delay(1000);
}
