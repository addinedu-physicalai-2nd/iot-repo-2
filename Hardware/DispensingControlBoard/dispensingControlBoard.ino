#include <WiFi.h>
#include <ArduinoJson.h>
#include <Stepper.h>
#include <ESP32Servo.h>

const char* wifiSsid = "addinedu_201class_2-2.4G";
const char* wifiPassword = "201class2!";
const char* serverIp = "192.168.0.225";
const uint16_t serverPort = 9002;


WiFiClient client;
String rxBuffer = "";

#define servo1Pin 16
#define servo2Pin 17
#define servo3Pin 18
#define servoSlotPin 26
#define sensorDispatchPin 35

const int stepsPerRevolution = 2048;
Stepper myStepper(stepsPerRevolution, 23, 21, 22, 19);

Servo servos[4];

const int homeAngle[3] = {0, 0, 175};
const int pushAngle[3] = {35, 35, 130};
const int slotAngle[3] = {70, 90, 110};

int currentAngle[4] = {0, 0, 0, 0};
int targetAngle[4] = {0, 0, 0, 0};
bool servoMoving[4] = {false, false, false, false};
unsigned long lastServoStepTime[4] = {0, 0, 0, 0};
const int servoStepDelay = 20;

int threshold = 2000;

bool wasDetected = false;
int dispatchCount = 0;
int expectedTotal = 0;
int currentOrderId = -1;

int dispensedByType[3] = {0, 0, 0};

#define maxQueue 30
int dispenseQueue[maxQueue];
int queueLen = 0;
int queueIndex = 0;

enum DispenseState { idle, push, returnState };
DispenseState dispenseState = idle;

bool conveyorRunning = false;

bool stopPending = false;
unsigned long stopPendingStart = 0;
const int stopGraceDelay = 800;

bool waitingForSensor = false;
unsigned long waitingStartTime = 0;
const unsigned long jamTimeout = 100000;

// ── Wi-Fi / TCP ──────────────────────────────────────

void connectWiFi()
{
  if (WiFi.status() == WL_CONNECTED)
  {
    return;
  }
  Serial.println("Wi-Fi 연결 시도 중...");
  WiFi.begin(wifiSsid, wifiPassword);
  while (WiFi.status() != WL_CONNECTED)
  {
    delay(300);
    Serial.print(".");
  }
  Serial.println("\nWi-Fi 연결됨: " + WiFi.localIP().toString());
}

void sendJson(JsonDocument& doc)
{
  String out;
  serializeJson(doc, out);
  client.println(out);
  Serial.print("[보낸 메시지] ");
  Serial.println(out);
}

void sendHello()
{
  StaticJsonDocument<64> doc;
  doc["hello"] = "dispenser";
  sendJson(doc);
}

void connectServer()
{
  if (client.connected())
  {
    return;
  }
  Serial.println("서버 연결 시도 중...");
  while (!client.connect(serverIp, serverPort))
  {
    delay(500);
    Serial.print(".");
  }
  Serial.println("\n서버 연결됨");
  sendHello();
}

void sendOrderComplete()
{
  StaticJsonDocument<192> doc;
  doc["event"] = "orderComplete";
  doc["orderId"] = currentOrderId;
  JsonArray arr = doc.createNestedArray("dispensed");
  arr.add(dispensedByType[0]);
  arr.add(dispensedByType[1]);
  arr.add(dispensedByType[2]);
  sendJson(doc);
}

void sendOrderFailed(const char* reason)
{
  StaticJsonDocument<192> doc;
  doc["event"] = "orderFailed";
  doc["orderId"] = currentOrderId;
  JsonArray arr = doc.createNestedArray("dispensed");
  arr.add(dispensedByType[0]);
  arr.add(dispensedByType[1]);
  arr.add(dispensedByType[2]);
  doc["reason"] = reason;
  sendJson(doc);
}

void sendOrderRejected(int orderId)
{
  StaticJsonDocument<128> doc;
  doc["event"] = "orderRejected";
  doc["orderId"] = orderId;
  doc["reason"] = "busy";
  sendJson(doc);
}

// ── 서보 이동 (비차단) ──────────────────────────────────

void setServoTarget(int idx, int angle)
{
  targetAngle[idx] = constrain(angle, 0, 180);
  servoMoving[idx] = (currentAngle[idx] != targetAngle[idx]);
}

void updateServos()
{
  for (int i = 0; i < 4; i++)
  {
    if (servoMoving[i] && millis() - lastServoStepTime[i] >= servoStepDelay)
    {
      lastServoStepTime[i] = millis();
      if (currentAngle[i] < targetAngle[i])
      {
        currentAngle[i]++;
      }
      else if (currentAngle[i] > targetAngle[i])
      {
        currentAngle[i]--;
      }
      servos[i].write(currentAngle[i]);
      if (currentAngle[i] == targetAngle[i])
      {
        servoMoving[i] = false;
      }
    }
  }
}

void moveSlotServo(int slot)
{
  setServoTarget(3, slotAngle[slot - 1]);
}

// ── 주문 처리 ───────────────────────────────────────────

void handleStartOrder(int orderId, int c0, int c1, int c2, int slot)
{
  queueLen = 0;
  for (int i = 0; i < c0 && queueLen < maxQueue; i++) dispenseQueue[queueLen++] = 0;
  for (int i = 0; i < c1 && queueLen < maxQueue; i++) dispenseQueue[queueLen++] = 1;
  for (int i = 0; i < c2 && queueLen < maxQueue; i++) dispenseQueue[queueLen++] = 2;
  queueIndex = 0;

  currentOrderId = orderId;
  expectedTotal = c0 + c1 + c2;
  dispatchCount = 0;
  dispensedByType[0] = 0;
  dispensedByType[1] = 0;
  dispensedByType[2] = 0;

  conveyorRunning = true;
  stopPending = false;
  waitingForSensor = false;

  Serial.printf("주문 시작: orderId=%d, queueLen=%d, slot=%d\n", orderId, queueLen, slot);

  moveSlotServo(slot);

  if (queueLen > 0)
  {
    int firstServo = dispenseQueue[0];
    setServoTarget(firstServo, pushAngle[firstServo]);
    dispenseState = push;
  }
}

// ── 서버 메시지 처리 ─────────────────────────────────────

void handleIncomingLine(String line)
{
  Serial.print("[받은 메시지] ");
  Serial.println(line);

  StaticJsonDocument<256> doc;
  DeserializationError err = deserializeJson(doc, line);
  if (err)
  {
    Serial.print("JSON 파싱 실패: ");
    Serial.println(err.c_str());
    return;
  }

  const char* cmd = doc["cmd"];
  Serial.print("cmd 값: ");
  Serial.println(cmd ? cmd : "(없음)");

  if (cmd != nullptr && strcmp(cmd, "startOrder") == 0)
  {
    int orderId = doc["orderId"];
    JsonArray counts = doc["counts"];
    int c0 = counts[0];
    int c1 = counts[1];
    int c2 = counts[2];
    int slot = doc["slot"];

    Serial.printf("startOrder 파싱됨: orderId=%d, counts=[%d,%d,%d], slot=%d\n",
                  orderId, c0, c1, c2, slot);

    if (dispenseState == idle && !conveyorRunning)
    {
      handleStartOrder(orderId, c0, c1, c2, slot);
    }
    else
    {
      Serial.println("바쁨 상태라 명령 거절됨");
      sendOrderRejected(orderId);
    }
  }
}

void setup()
{
  Serial.begin(115200);
  delay(500);

  pinMode(sensorDispatchPin, INPUT);

  ESP32PWM::allocateTimer(0);
  ESP32PWM::allocateTimer(1);
  ESP32PWM::allocateTimer(2);
  ESP32PWM::allocateTimer(3);

  servos[0].setPeriodHertz(50);
  servos[0].attach(servo1Pin, 500, 2400);
  servos[0].write(homeAngle[0]);
  currentAngle[0] = homeAngle[0];

  servos[1].setPeriodHertz(50);
  servos[1].attach(servo2Pin, 500, 2400);
  servos[1].write(homeAngle[1]);
  currentAngle[1] = homeAngle[1];

  servos[2].setPeriodHertz(50);
  servos[2].attach(servo3Pin, 500, 2400);
  servos[2].write(homeAngle[2]);
  currentAngle[2] = homeAngle[2];

  servos[3].setPeriodHertz(50);
  servos[3].attach(servoSlotPin, 500, 2400);
  servos[3].write(90);
  currentAngle[3] = 90;

  myStepper.setSpeed(14);

  connectWiFi();
  connectServer();

  Serial.println("=== Dispensing 보드 준비 완료 (TCP) ===");
}

void loop()
{
  if (WiFi.status() != WL_CONNECTED)
  {
    connectWiFi();
  }
  if (!client.connected())
  {
    connectServer();
  }

  if (conveyorRunning)
  {
    myStepper.step(20);
  }

  updateServos();

  while (client.available())
  {
    char c = client.read();
    if (c == '\n' || c == '\r')
    {
      if (rxBuffer.length() > 0)
      {
        handleIncomingLine(rxBuffer);
        rxBuffer = "";
      }
    }
    else
    {
      rxBuffer += c;
    }
  }

  int currentServo = dispenseQueue[queueIndex];

  if (dispenseState == push && !servoMoving[currentServo])
  {
    setServoTarget(currentServo, homeAngle[currentServo]);
    dispenseState = returnState;
  }
  else if (dispenseState == returnState && !servoMoving[currentServo])
  {
    queueIndex++;
    if (queueIndex < queueLen)
    {
      int nextServo = dispenseQueue[queueIndex];
      setServoTarget(nextServo, pushAngle[nextServo]);
      dispenseState = push;
    }
    else
    {
      dispenseState = idle;
      waitingForSensor = true;
      waitingStartTime = millis();
      Serial.println("배출 큐 전부 처리 완료, 센서 대기 시작");
    }
  }

  int d1 = analogRead(sensorDispatchPin);
  bool isDetected = (d1 < threshold);
  if (isDetected && !wasDetected)
  {
    if (dispatchCount < queueLen)
    {
      int typeJustPassed = dispenseQueue[dispatchCount];
      dispensedByType[typeJustPassed]++;
    }
    dispatchCount++;

    Serial.printf("물체 통과 감지: %d/%d\n", dispatchCount, expectedTotal);

    if (expectedTotal > 0 && dispatchCount == expectedTotal)
    {
      expectedTotal = 0;
      waitingForSensor = false;
      stopPending = true;
      stopPendingStart = millis();
    }
  }
  wasDetected = isDetected;

  if (waitingForSensor && millis() - waitingStartTime >= jamTimeout)
  {
    waitingForSensor = false;
    conveyorRunning = false;
    Serial.println("잼(jam) 감지: 타임아웃 도달");
    sendOrderFailed("jam");
    currentOrderId = -1;
  }

  if (stopPending && millis() - stopPendingStart >= stopGraceDelay)
  {
    conveyorRunning = false;
    stopPending = false;
    Serial.println("출고 완료");
    sendOrderComplete();
    currentOrderId = -1;
  }
}