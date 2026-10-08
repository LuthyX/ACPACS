/*
 * ACPACS — Gate Controller Firmware v3.0
 * Microcontroller: ATmega328P (Arduino Uno)
 *
 * v3.0 changes:
 *   A. Emits a TEMPLATE, not an id — FP_OK:<hex>:<confidence>
 *      Matches the BLOB session-binding model in report S3.3.2.2 / S3.4.1
 *      Table 3.3, where the template is stored in the session row and the
 *      exit check is a single 1:1 comparison.
 *   B. New FULL command — CAR PARK FULL display for the capacity check
 *      specified in report S3.4.2.
 *
 * v2.1 changes (all gated behind WOKWI_SIM):
 *   1. WOKWI_SIM / FP_BUTTON / FP_ATTEMPTS defines
 *   2. setup(): skip R307 handshake in sim, arm the FINGER button instead
 *   3. scanFingerprint(): button stands in for the sensor in sim;
 *      added the MISSING finger.fingerFastSearch() on the hardware path
 *   4. beep(): tone() in sim (Wokwi's buzzer is passive)
 *
 * Components:
 *   - IR Sensor Entry    (pin 4)
 *   - IR Sensor Exit     (pin 8)
 *   - R307 Fingerprint   (pins 2, 3 SoftwareSerial)
 *   - Servo Motor        (pin 9)
 *   - 16x2 LCD I2C       (SDA=A4, SCL=A5)
 *   - Green LED          (pin 5)
 *   - Red LED            (pin 6)
 *   - Buzzer             (pin 7)
 *   - FINGER button      (pin 12)  <-- simulation only
 *
 * Full Flow:
 *   1. IR sensor detects car -> Arduino sends CAR_AT_ENTRY/EXIT to Python
 *   2. Python runs ALPR -> sends PLATE:xxx back to Arduino
 *   3. Arduino prompts fingerprint scan
 *   4. Fingerprint scanned -> Arduino sends FP_OK:TOKEN to Python
 *   5. Python verifies with backend -> sends OPEN or DENY
 *   6. Arduino operates gate + LCD + LEDs
 *
 * Python -> Arduino Commands:
 *   PLATE:KSF487HN   display plate on LCD
 *   ENTRY            prompt fingerprint for entry
 *   EXIT             prompt fingerprint for exit
 *   OPEN             open gate
 *   DENY             deny access
 *   FULL             car park at capacity (S3.4.2)
 *   BILL:500         display bill on LCD
 *
 * Arduino -> Python Commands:
 *   READY            system initialized
 *   CAR_AT_ENTRY     IR triggered at entry
 *   CAR_AT_EXIT      IR triggered at exit
 *   FP_OK:<hex>:<conf>   template captured, hex-encoded
 *   FP_FAIL              no finger presented
 */

#include <Servo.h>
#include <LiquidCrystal_I2C.h>
#include <Adafruit_Fingerprint.h>
#include <SoftwareSerial.h>

// ===== HUNK 1 =============================================================
// Comment out this ONE line when flashing real hardware.
#define WOKWI_SIM

// A real R307 minutiae template is 512 bytes. Shipping 1024 hex characters
// through a 2KB-SRAM ATmega328P over SoftwareSerial is not viable, so the
// simulator emits a shorter stand-in. Chapter 4 must state this.
#define FP_TEMPLATE_BYTES 32

#ifdef WOKWI_SIM
  #define FP_BUTTON    12    // blue pushbutton stands in for the R307
  #define FP_ATTEMPTS  75    // 75 x 200ms = 15s to press it during a demo
#else
  #define FP_ATTEMPTS  15    // original: 15 x 200ms
#endif
// ==========================================================================

#define IR_ENTRY   4
#define IR_EXIT    8
#define GREEN_LED  5
#define RED_LED    6
#define BUZZER     7
#define SERVO_PIN  9
#define GATE_CLOSED 0
#define GATE_OPEN   90
#define DEBOUNCE_MS 2000

SoftwareSerial fpSerial(2, 3);
Adafruit_Fingerprint finger = Adafruit_Fingerprint(&fpSerial);
Servo gateServo;
LiquidCrystal_I2C lcd(0x27, 16, 2);

String currentPlate = "";
String currentMode  = "";
unsigned long lastEntryTrigger = 0;
unsigned long lastExitTrigger  = 0;


void setup() {
  Serial.begin(9600);

  pinMode(IR_ENTRY,  INPUT_PULLUP);
  pinMode(IR_EXIT,   INPUT_PULLUP);
  pinMode(GREEN_LED, OUTPUT);
  pinMode(RED_LED,   OUTPUT);
  pinMode(BUZZER,    OUTPUT);

  digitalWrite(GREEN_LED, LOW);
  digitalWrite(RED_LED,   LOW);

  gateServo.attach(SERVO_PIN);
  gateServo.write(GATE_CLOSED);

  lcd.init();
  lcd.backlight();
  lcdPrint("ACPACS v3.0", "Initializing...");

  // ===== HUNK 2 ===========================================================
#ifdef WOKWI_SIM
  pinMode(FP_BUTTON, INPUT_PULLUP);
  lcdPrint("FP Sensor", "SIMULATED");
#else
  finger.begin(57600);
  delay(500);

  if (finger.verifyPassword()) {
    lcdPrint("FP Sensor", "OK");
  } else {
    lcdPrint("FP Sensor", "ERROR");
    flashLED(RED_LED, 5);
  }
#endif
  // ========================================================================

  delay(1000);
  lcdPrint("System Ready", "Waiting...");
  Serial.println("READY");
}


void loop() {
  unsigned long now = millis();

  if (digitalRead(IR_ENTRY) == LOW && (now - lastEntryTrigger) > DEBOUNCE_MS) {
    lastEntryTrigger = now;
    lcdPrint("Car Detected", "Entry Gate");
    beep(1, 100);
    Serial.println("CAR_AT_ENTRY");
  }

  if (digitalRead(IR_EXIT) == LOW && (now - lastExitTrigger) > DEBOUNCE_MS) {
    lastExitTrigger = now;
    lcdPrint("Car Detected", "Exit Gate");
    beep(1, 100);
    Serial.println("CAR_AT_EXIT");
  }

  if (Serial.available()) {
    String cmd = Serial.readStringUntil('\n');
    cmd.trim();
    if (cmd.length() > 0) handleCommand(cmd);
  }
}


void handleCommand(String cmd) {
  if (cmd.startsWith("PLATE:")) {
    currentPlate = cmd.substring(6);
    lcdPrint("Plate:", currentPlate);
  }
  else if (cmd == "ENTRY") {
    currentMode = "ENTRY";
    lcdPrint("Place Finger", "on Scanner...");
    scanFingerprint();
  }
  else if (cmd == "EXIT") {
    currentMode = "EXIT";
    lcdPrint("Place Finger", "to Exit...");
    scanFingerprint();
  }
  else if (cmd == "OPEN")  { openGate(); }
  else if (cmd == "DENY")  { denyAccess(); }
  else if (cmd == "FULL")  { parkFull(); }
  else if (cmd.startsWith("BILL:")) {
    String amount = cmd.substring(5);
    lcdPrint("Bill: N" + amount, "Thank you!");
    beep(2, 150);
    delay(3000);
    lcdPrint("System Ready", "Waiting...");
  }
}


// ===== HUNK 3 =============================================================
// Emit a fingerprint TEMPLATE rather than an enrolled id.
//
// Report S3.3.2.2 specifies that the template captured at entry is stored in
// the session row and compared 1:1 against the live scan at exit. That makes
// the exit check a single-row lookup, independent of how many cars are parked,
// and means nothing is ever written to the sensor's onboard library.
//
// Wire format:  FP_OK:<hex template>:<confidence>
void emitTemplate(int personId, int conf) {
  // Deterministic synthetic template: the same simulated person always
  // produces the same bytes, so the exit comparison is meaningful.
  // On real hardware this is replaced by an UpChar read from the sensor.
  randomSeed((unsigned long)personId * 7919UL);

  Serial.print("FP_OK:");
  for (int i = 0; i < FP_TEMPLATE_BYTES; i++) {
    byte b = (byte)random(256);
    if (b < 16) Serial.print('0');     // keep every byte two hex digits
    Serial.print(b, HEX);
  }
  Serial.print(':');
  Serial.println(conf);
}

void scanFingerprint() {
  int attempts = 0;
  while (attempts < FP_ATTEMPTS) {
    bool matched = false;
    int  person  = 0;
    int  conf    = 0;

#ifdef WOKWI_SIM
    if (digitalRead(FP_BUTTON) == LOW) {
      delay(50);                                        // debounce
      unsigned long held = millis();
      while (digitalRead(FP_BUTTON) == LOW) delay(10);  // wait for release
      held = millis() - held;

      matched = true;
      // Hold the button 1.5s+ to simulate a DIFFERENT person's finger.
      // Lets you demonstrate the exit gate refusing the wrong driver.
      person  = (held >= 1500) ? 2 : 1;
      conf    = 88;
    }
#else
    // TODO (hardware phase): report S3.3.2.1 requires the actual 512-byte
    // template here, read off the sensor with the UpChar instruction. The
    // Adafruit library does not expose that, so it needs custom packet code.
    // Until then this derives a placeholder from the matched slot, which is
    // NOT a real minutiae template. Do not claim otherwise in Chapter 4.
    if (finger.getImage()         == FINGERPRINT_OK &&
        finger.image2Tz()         == FINGERPRINT_OK &&
        finger.fingerFastSearch() == FINGERPRINT_OK) {
      matched = true;
      person  = finger.fingerID;
      conf    = finger.confidence;
    }
#endif

    if (matched) {
      digitalWrite(GREEN_LED, HIGH);
      beep(1, 100);
      digitalWrite(GREEN_LED, LOW);
      lcdPrint("FP Captured", "Processing...");
      emitTemplate(person, conf);
      return;
    }

    delay(200);
    attempts++;
  }

  flashLED(RED_LED, 3);
  beep(3, 200);
  lcdPrint("No Finger", "Detected");
  Serial.println("FP_FAIL");
  delay(2000);
  lcdPrint("System Ready", "Waiting...");
}
// ==========================================================================


void openGate() {
  digitalWrite(GREEN_LED, HIGH);
  digitalWrite(RED_LED,   LOW);
  beep(1, 400);
  if (currentMode == "ENTRY") lcdPrint("Welcome!", currentPlate);
  else                        lcdPrint("Safe Journey!", currentPlate);
  gateServo.write(GATE_OPEN);
  delay(5000);
  gateServo.write(GATE_CLOSED);
  digitalWrite(GREEN_LED, LOW);
  lcdPrint("System Ready", "Waiting...");
}


// Capacity management, report S3.4.2. Sent when active sessions have
// reached the configured maximum occupancy n.
void parkFull() {
  digitalWrite(RED_LED,   HIGH);
  digitalWrite(GREEN_LED, LOW);
  beep(2, 300);
  lcdPrint("CAR PARK FULL", "No spaces left");
  delay(3000);
  digitalWrite(RED_LED, LOW);
  lcdPrint("System Ready", "Waiting...");
}


void denyAccess() {
  digitalWrite(RED_LED,   HIGH);
  digitalWrite(GREEN_LED, LOW);
  beep(3, 150);
  lcdPrint("Access Denied", "Contact Admin");
  delay(3000);
  digitalWrite(RED_LED, LOW);
  lcdPrint("System Ready", "Waiting...");
}


void lcdPrint(String line1, String line2) {
  lcd.clear();
  lcd.setCursor(0, 0);
  lcd.print(line1.substring(0, 16));
  lcd.setCursor(0, 1);
  lcd.print(line2.substring(0, 16));
}


// ===== HUNK 4 =============================================================
void beep(int times, int duration) {
  for (int i = 0; i < times; i++) {
#ifdef WOKWI_SIM
    tone(BUZZER, 2000);          // Wokwi's buzzer is passive — needs a frequency
    delay(duration);
    noTone(BUZZER);
#else
    digitalWrite(BUZZER, HIGH); delay(duration);
    digitalWrite(BUZZER, LOW);
#endif
    if (i < times - 1) delay(100);
  }
}
// ==========================================================================


void flashLED(int pin, int times) {
  for (int i = 0; i < times; i++) {
    digitalWrite(pin, HIGH); delay(200);
    digitalWrite(pin, LOW);  delay(200);
  }
}
