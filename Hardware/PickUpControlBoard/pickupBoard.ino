/*
  픽업 스탠드 보드 - IR센서 3개로 슬롯 점유 상태 감지
  노트북(메인서버)과 USB Serial로 통신 (전원도 이 USB로 받음)
*/

#include <ArduinoJson.h>

#define sensorSlot1Pin 32
#define sensorSlot2Pin 33
#define sensorSlot3Pin 34

int threshold = 2000; // 실제 값 확인 후 조정

bool slotOccupied[3] = {false, false, false};

unsigned long lastReportTime = 0;
const unsigned long reportInterval = 300; // 상태 보고 주기(ms)

void sendSlotState(int slotNumber, bool occupied)
{
  StaticJsonDocument<128> doc;
  doc["event"] = "slotState";
  doc["boardId"] = "pickup";
  doc["slot"] = slotNumber;
  doc["occupied"] = occupied;
  serializeJson(doc, Serial);
  Serial.println(); // 개행으로 메시지 끝 표시
}

void sendHello()
{
  StaticJsonDocument<64> doc;
  doc["hello"] = "pickup";
  serializeJson(doc, Serial);
  Serial.println();
}

void setup()
{
  Serial.begin(115200);
  delay(500);

  pinMode(sensorSlot1Pin, INPUT);
  pinMode(sensorSlot2Pin, INPUT);
  pinMode(sensorSlot3Pin, INPUT);

  sendHello(); // 서버에 "나 픽업보드야" 알림
}

void checkSlot(int slotIndex, int pin)
{
  int value = analogRead(pin);
  bool occupied = (value < threshold);

  if (occupied != slotOccupied[slotIndex])
  {
    slotOccupied[slotIndex] = occupied;
    sendSlotState(slotIndex + 1, occupied); // 슬롯 번호는 1부터
  }
}

void loop()
{
  checkSlot(0, sensorSlot1Pin);
  checkSlot(1, sensorSlot2Pin);
  checkSlot(2, sensorSlot3Pin);

  delay(100); // 센서 폴링 주기
}
