#include <Arduino.h>
#include <Wire.h>
#include <U8g2lib.h>
#include <Adafruit_TinyUSB.h>

// ======================================================
// ===================== USB HID ========================
// ======================================================
// Report ID 1 = Keyboard
// Report ID 2 = Consumer (Media)

uint8_t const hid_report_descriptor[] = {
  TUD_HID_REPORT_DESC_KEYBOARD(HID_REPORT_ID(1)),
  TUD_HID_REPORT_DESC_CONSUMER(HID_REPORT_ID(2))
};

Adafruit_USBD_HID usb_hid(
  hid_report_descriptor,
  sizeof(hid_report_descriptor),
  HID_ITF_PROTOCOL_NONE,
  2,
  true
);

bool hidEnabled = true;

// ======================================================
// ======================= OLED =========================
// ======================================================
U8G2_SH1106_128X64_NONAME_F_HW_I2C u8g2(U8G2_R0, U8X8_PIN_NONE);

// ======================================================
// ======================== PINS ========================
// ======================================================
const int BTN_PINS[4] = { D7, D8, D9, D10 };
const int ENC_A  = D1;
const int ENC_B  = D0;
const int ENC_SW = D2;

// ======================================================
// ======================== LOGO ========================
// ======================================================
const unsigned char epd_bitmap_vortex_no_text [] PROGMEM = {
  0xf0,0x00,0x00,0xe0,0x00,0x00,0xe0,0x00,0x00,0x1d,0x80,0x00,
  0x1d,0x80,0x00,0x1d,0xbe,0x60,0x01,0xbe,0xa0,0x01,0xbd,0x00,
  0x01,0x80,0x40,0x01,0x82,0x80,0x01,0x85,0x00,0x01,0x8e,0x00,
  0x01,0x8e,0x00,0x01,0x9c,0x00,0x01,0xb8,0x00,0x01,0xf8,0x00,
  0x01,0xf0,0x00,0x01,0xe0,0x00,0x01,0xc0,0x00,0x01,0xc0,0x00
};

#define LOGO_W 20
#define LOGO_H 20

// ======================================================
// ================= BITMAP UTILS =======================
// ======================================================
static inline uint8_t bitrev(uint8_t b){
  b = (b >> 4) | (b << 4);
  b = ((b & 0xCC) >> 2) | ((b & 0x33) << 2);
  b = ((b & 0xAA) >> 1) | ((b & 0x55) << 1);
  return b;
}

void drawMSBBitmap(int x,int y,uint8_t w,uint8_t h,const uint8_t* src){
  const uint8_t bpr = (w + 7) / 8;
  const uint16_t sz = bpr * h;
  static uint8_t buf[512];
  if (sz > sizeof(buf)) return;
  for (uint16_t i = 0; i < sz; i++) buf[i] = bitrev(src[i]);
  u8g2.drawXBMP(x, y, w, h, buf);
}

// ======================================================
// ======================= UI ===========================
// ======================================================
enum UiState { UI_SPLASH, UI_HOME };
UiState uiState = UI_SPLASH;

unsigned long stateTimer;
const unsigned long SPLASH_TIME = 1200;

void drawSplash(){
  u8g2.clearBuffer();
  drawMSBBitmap(
    (128 - LOGO_W) / 2,
    (64  - LOGO_H) / 2,
    LOGO_W,
    LOGO_H,
    epd_bitmap_vortex_no_text
  );
  u8g2.sendBuffer();
}

// ======================================================
// ===================== HID HELPERS ====================
// ======================================================
void sendKey(uint8_t keycode){
  uint8_t report[8] = {0};
  report[2] = keycode;
  usb_hid.sendReport(1, report, sizeof(report));
}

void releaseKey(){
  uint8_t report[8] = {0};
  usb_hid.sendReport(1, report, sizeof(report));
}

void sendConsumer(uint16_t usage){
  usb_hid.sendReport(2, &usage, sizeof(usage));
}

void releaseConsumer(){
  uint16_t empty = 0;
  usb_hid.sendReport(2, &empty, sizeof(empty));
}

// ======================================================
// ===================== BUTTONS ========================
// ======================================================
struct ButtonState { bool pressed; };
ButtonState buttons[4];

// A S L ;
uint8_t keycodes[4] = {
  HID_KEY_A,
  HID_KEY_S,
  HID_KEY_L,
  HID_KEY_SEMICOLON
};

// ======================================================
// ====================== TRAILS ========================
// ======================================================
#define MAX_TRAILS 24
struct Trail {
  bool active;
  uint8_t btn;
  int x, y, height;
  bool held;
};
Trail trails[MAX_TRAILS];

void spawnTrail(uint8_t btn){
  for (int i=0;i<MAX_TRAILS;i++){
    if (!trails[i].active){
      trails[i] = {true, btn, 48 + btn*20, 48, 4, true};
      return;
    }
  }
}

// ======================================================
// ======================== KPS =========================
// ======================================================
unsigned long keyTimes[32];
uint8_t keyIndex = 0;

float getKPS(){
  unsigned long now = millis();
  int c = 0;
  for (int i=0;i<32;i++)
    if (now - keyTimes[i] <= 1000) c++;
  return c;
}

// ======================================================
// ===================== ENCODER ========================
// ======================================================
volatile int encoderPos = 0;
volatile uint8_t lastEncState = 0;
int lastReportedPos = 0;
int encDir = 0;
unsigned long encIconTimer = 0;

bool encPressed = false;

void encoderISR(){
  uint8_t state = (digitalRead(ENC_A)<<1) | digitalRead(ENC_B);
  static const int8_t table[16] = {
     0,-1, 1, 0,
     1, 0, 0,-1,
    -1, 0, 0, 1,
     0, 1,-1, 0
  };
  encoderPos -= table[(lastEncState<<2)|state];
  lastEncState = state;
}

// ======================================================
// ====================== INPUT =========================
// ======================================================
void scanButtons(){
  for (int i=0;i<4;i++){
    bool now = !digitalRead(BTN_PINS[i]);

    if (now && !buttons[i].pressed){
      keyTimes[keyIndex++ % 32] = millis();
      spawnTrail(i);
      if (hidEnabled) sendKey(keycodes[i]);
    }

    if (!now && buttons[i].pressed){
      if (hidEnabled) releaseKey();
      for (int t=MAX_TRAILS-1;t>=0;t--){
        if (trails[t].active && trails[t].btn==i && trails[t].held){
          trails[t].held=false;
          break;
        }
      }
    }

    buttons[i].pressed = now;
  }
}

void scanEncoderButton(){
  bool now = !digitalRead(ENC_SW);

  if (now && !encPressed){
    sendConsumer(HID_USAGE_CONSUMER_PLAY_PAUSE);
  }

  if (!now && encPressed){
    releaseConsumer();
  }

  encPressed = now;
}

void readEncoderUI(){
  int detent = encoderPos / 4;
  if (detent != lastReportedPos){
    encDir = (detent > lastReportedPos) ? +1 : -1;
    lastReportedPos = detent;
    encIconTimer = millis();

    if (hidEnabled){
      uint16_t usage = encDir > 0
        ? HID_USAGE_CONSUMER_VOLUME_INCREMENT
        : HID_USAGE_CONSUMER_VOLUME_DECREMENT;
      sendConsumer(usage);
      delay(5);
      releaseConsumer();
    }
  }
}

void updateTrails(){
  for (int i=0;i<MAX_TRAILS;i++){
    if (!trails[i].active) continue;
    if (trails[i].held){
      if (trails[i].height < 48) trails[i].height += 2;
    } else {
      trails[i].y -= 3;
      if (trails[i].y + trails[i].height < 0)
        trails[i].active=false;
    }
  }
}

// ======================================================
// ====================== DRAW ==========================
// ======================================================
void drawHome(){
  u8g2.clearBuffer();

  u8g2.drawFrame(0,0,40,64);
  drawMSBBitmap(10,6,LOGO_W,LOGO_H,epd_bitmap_vortex_no_text);

  u8g2.setCursor(6,42); u8g2.print("KPS");
  u8g2.setCursor(6,56); u8g2.print(getKPS(),1);

  if (millis() - encIconTimer < 2000){
    u8g2.setCursor(118,12);   // TOP-RIGHT
    u8g2.print(encDir > 0 ? "+" : "-");
  }

  for (int i=0;i<MAX_TRAILS;i++){
    if (!trails[i].active) continue;
    u8g2.drawBox(trails[i].x, trails[i].y - trails[i].height, 16, trails[i].height);
  }

  const char* labels[4] = { "A", "S", "L", ";" };
  for (int i=0;i<4;i++){
    int bx = 48 + i*20;
    u8g2.drawFrame(bx,48,16,16);
    u8g2.setCursor(bx+5,60);
    u8g2.print(labels[i]);
  }

  u8g2.sendBuffer();
}

// ======================================================
// ====================== SETUP =========================
// ======================================================
void setup(){
  for (int i=0;i<4;i++) pinMode(BTN_PINS[i], INPUT_PULLUP);
  pinMode(ENC_A, INPUT_PULLUP);
  pinMode(ENC_B, INPUT_PULLUP);
  pinMode(ENC_SW, INPUT_PULLUP);

  attachInterrupt(digitalPinToInterrupt(ENC_A), encoderISR, CHANGE);
  attachInterrupt(digitalPinToInterrupt(ENC_B), encoderISR, CHANGE);

  u8g2.begin();
  u8g2.setFont(u8g2_font_6x12_tf);

  usb_hid.begin();
  while (!TinyUSBDevice.mounted()) delay(10);

  stateTimer = millis();
}

// ======================================================
// ======================= LOOP =========================
// ======================================================
void loop(){
  if (uiState == UI_SPLASH){
    drawSplash();
    if (millis() - stateTimer > SPLASH_TIME)
      uiState = UI_HOME;
    return;
  }

  readEncoderUI();
  scanEncoderButton();
  scanButtons();
  updateTrails();
  drawHome();
}
