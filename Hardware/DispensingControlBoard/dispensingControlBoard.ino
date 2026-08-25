#include <WiFi.h>
#include <Stepper.h>
#include <ESP32Servo.h>

/*
  서버(:9002)와의 통신은 바이너리 프레임이다.  ★ Library/protocol.py 와 반드시 같아야 함

      [ TAG (ASCII 2바이트) ][ PAYLOAD (태그마다 길이 고정) ]

  길이 필드가 없다. 태그를 보면 뒤에 몇 바이트가 오는지 알 수 있기 때문이다.
  숫자는 빅엔디안(네트워크 바이트 순서), orderId 만 2바이트고 나머지는 1바이트.

    서버 → 보드
      SO  startOrder      orderId(2) counts(3) slot(1)       = 6
    보드 → 서버
      HL  hello           (없음)                              = 0
      OC  orderComplete   orderId(2) dispensed(3)             = 5
      OF  orderFailed     orderId(2) dispensed(3) reason(1)   = 6
      OR  orderRejected   orderId(2) reason(1)                = 3
*/
#define TAG_SIZE 2
#define PAYLOAD_START_ORDER 6
#define PAYLOAD_ORDER_COMPLETE 5
#define PAYLOAD_ORDER_FAILED 6
#define PAYLOAD_ORDER_REJECTED 3

// reason 코드 — protocol.py 의 FAIL_REASON_CODE 와 같은 값
#define REASON_UNKNOWN 0x00
#define REASON_JAM 0x01
#define REASON_BOARD_TIMEOUT 0x02
#define REASON_BOARD_RESET 0x03
#define REASON_BUSY 0x04

const char* wifiSsid = "addinedu_201class_2-2.4G";
const char* wifiPassword = "201class2!";
const char* serverIp = "192.168.0.225";
const uint16_t serverPort = 9002;


WiFiClient client;

// 수신 버퍼. 프레임 하나는 최대 8B(태그2+페이로드6)지만, 조각나서 오거나
// 여러 개가 붙어 와도 담기게 넉넉히 잡는다.
uint8_t rxBuf[32];
uint8_t rxLen = 0;

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

// 태그 + 페이로드를 한 번에 write 한다(두 번 나눠 쓰면 세그먼트가 갈려
// 지연만 늘어난다). 서버는 스트림을 다시 붙이지만 굳이 쪼갤 이유가 없다.
void sendFrame(const char* tag, const uint8_t* payload, uint8_t len)
{
  uint8_t frame[TAG_SIZE + 6];
  frame[0] = (uint8_t)tag[0];
  frame[1] = (uint8_t)tag[1];
  for (uint8_t i = 0; i < len; i++)
  {
    frame[TAG_SIZE + i] = payload[i];
  }
  client.write(frame, TAG_SIZE + len);
  Serial.printf("[보낸 프레임] %c%c (%u바이트)\n", tag[0], tag[1], len);
}

// orderId 는 uint16. 아직 주문이 없을 때(-1)는 0 으로 내보낸다.
uint16_t safeOrderId()
{
  return (currentOrderId < 0) ? 0 : (uint16_t)currentOrderId;
}

void sendHello()
{
  sendFrame("HL", nullptr, 0);
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
  uint16_t id = safeOrderId();
  uint8_t payload[PAYLOAD_ORDER_COMPLETE] = {
      (uint8_t)(id >> 8), (uint8_t)(id & 0xFF),
      (uint8_t)dispensedByType[0],
      (uint8_t)dispensedByType[1],
      (uint8_t)dispensedByType[2]};
  sendFrame("OC", payload, PAYLOAD_ORDER_COMPLETE);
}

void sendOrderFailed(uint8_t reason)
{
  uint16_t id = safeOrderId();
  uint8_t payload[PAYLOAD_ORDER_FAILED] = {
      (uint8_t)(id >> 8), (uint8_t)(id & 0xFF),
      (uint8_t)dispensedByType[0],
      (uint8_t)dispensedByType[1],
      (uint8_t)dispensedByType[2],
      reason};
  sendFrame("OF", payload, PAYLOAD_ORDER_FAILED);
}

void sendOrderRejected(uint16_t orderId)
{
  uint8_t payload[PAYLOAD_ORDER_REJECTED] = {
      (uint8_t)(orderId >> 8), (uint8_t)(orderId & 0xFF),
      REASON_BUSY};
  sendFrame("OR", payload, PAYLOAD_ORDER_REJECTED);
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

// 이 보드가 받는 명령은 SO(startOrder) 하나뿐이다.
// 아는 태그면 페이로드 길이를, 모르는 태그면 -1 을 돌려준다.
int payloadLenFor(const uint8_t* tag)
{
  if (tag[0] == 'S' && tag[1] == 'O')
  {
    return PAYLOAD_START_ORDER;
  }
  return -1;
}

void handleStartOrderFrame(const uint8_t* payload)
{
  uint16_t orderId = ((uint16_t)payload[0] << 8) | payload[1];
  int c0 = payload[2];
  int c1 = payload[3];
  int c2 = payload[4];
  int slot = payload[5];

  Serial.printf("startOrder 수신: orderId=%u, counts=[%d,%d,%d], slot=%d\n",
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

// 받은 바이트를 모아 프레임 단위로 잘라낸다.
// TCP 는 스트림이라 프레임이 쪼개져 오거나 여러 개가 붙어 온다.
// 예전 '\n' 자르기가 하던 일을 이게 대신한다.
void readServerFrames()
{
  while (client.available())
  {
    if (rxLen >= sizeof(rxBuf))
    {
      Serial.println("수신 버퍼 넘침 — 비우고 재동기화");
      rxLen = 0;
    }
    rxBuf[rxLen++] = (uint8_t)client.read();

    while (rxLen >= TAG_SIZE)
    {
      int need = payloadLenFor(rxBuf);
      if (need < 0)
      {
        // 모르는 태그 = 스트림 어긋남. 구분자가 없으니 1바이트씩 버리며
        // 다시 맞춘다(그대로 두면 영영 못 읽는다).
        rxLen--;
        memmove(rxBuf, rxBuf + 1, rxLen);
        continue;
      }
      if (rxLen < TAG_SIZE + need)
      {
        break;                       // 페이로드가 아직 덜 왔다
      }
      handleStartOrderFrame(rxBuf + TAG_SIZE);
      uint8_t used = TAG_SIZE + need;
      rxLen -= used;
      memmove(rxBuf, rxBuf + used, rxLen);
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

  readServerFrames();

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
    sendOrderFailed(REASON_JAM);
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