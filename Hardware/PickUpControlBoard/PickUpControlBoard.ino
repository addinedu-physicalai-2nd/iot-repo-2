#include <SPI.h>
#include <MFRC522.h>

// ESP32-DevKit RFID 핀 설정
const int SS_PIN = 5;
const int RST_PIN = 22;
const int SCK_PIN = 18;
const int MISO_PIN = 19;
const int MOSI_PIN = 23;

MFRC522 rc522(SS_PIN, RST_PIN);

// RFID 잔액 저장 블록
const int TOTAL_INDEX = 60;
MFRC522::MIFARE_Key key;

// IR 슬롯 센서
const int sensorPins[3] = {32, 33, 34};
int threshold = 2000;
bool slotOccupied[3] = {false, false, false};

// RFID 인증
MFRC522::StatusCode checkAuth(int index)
{
  MFRC522::StatusCode status = rc522.PCD_Authenticate(MFRC522::PICC_CMD_MF_AUTH_KEY_A, index, &key, &(rc522.uid));
  return status;
}

// RFID 데이터 읽기
MFRC522::StatusCode readData(int index, byte* data)
{
  MFRC522::StatusCode status = checkAuth(index);

  if (status != MFRC522::STATUS_OK)
  {
    return status;
  }

  byte buffer[18];
  byte length = 18;

  status = rc522.MIFARE_Read(index, buffer, &length);

  if (status == MFRC522::STATUS_OK)
  {
    memcpy(data, buffer, 4);
  }

  return status;
}

// RFID 데이터 쓰기
MFRC522::StatusCode writeData(int index, byte* data, int length)
{
  MFRC522::StatusCode status = checkAuth(index);

  if (status != MFRC522::STATUS_OK)
  {
    return status;
  }

  byte buffer[16];
  memset(buffer, 0x00, sizeof(buffer));
  memcpy(buffer, data, length);

  status = rc522.MIFARE_Write(index, buffer, 16);
  return status;
}

// 보드 연결 알림
// HI + boardId
void sendHello()
{
  char buf[3];

  buf[0] = 'H';
  buf[1] = 'I';
  buf[2] = 0x01;

  Serial.write(buf, 3);
  Serial.println();
}

// 슬롯 상태 전송
// SL + slot + occupied
void sendSlotState(int slotNumber, bool occupied)
{
  char buf[4];

  buf[0] = 'S';
  buf[1] = 'L';
  buf[2] = (char)slotNumber;
  buf[3] = occupied ? 0x01 : 0x00;

  Serial.write(buf, 4);
  Serial.println();
}

// 슬롯 상태 확인
void checkSlots()
{
  for (int i = 0; i < 3; i++)
  {
    int value = analogRead(sensorPins[i]);
    bool occupied = (value < threshold);

    if (occupied != slotOccupied[i])
    {
      slotOccupied[i] = occupied;
      sendSlotState(i + 1, occupied);
    }
  }
}

void setup()
{
  Serial.begin(115200);
  Serial.setTimeout(50);

  // RFID 기본 Key
  for (int i = 0; i < 6; i++)
  {
    key.keyByte[i] = 0xFF;
  }

  // IR 센서 입력 설정
  for (int i = 0; i < 3; i++)
  {
    pinMode(sensorPins[i], INPUT);
  }

  // ESP32 SPI 초기화
  SPI.begin(SCK_PIN, MISO_PIN, MOSI_PIN, SS_PIN);

  // MFRC522 초기화
  rc522.PCD_Init();

  delay(1000);

  // 보드 연결 알림
  sendHello();
}

void loop()
{
  int recv_size = 0;
  char recv_buffer[16];

  memset(recv_buffer, 0x00, sizeof(recv_buffer));

  // Serial 명령 수신
  if (Serial.available() > 0)
  {
    recv_size = Serial.readBytesUntil('\n', recv_buffer, sizeof(recv_buffer));
  }

  // RFID 카드 확인
  bool newCard = rc522.PICC_IsNewCardPresent();
  bool readCard = false;

  if (newCard == true)
  {
    readCard = rc522.PICC_ReadCardSerial();
  }

  // 명령이 들어온 경우
  if (recv_size > 0)
  {
    char cmd[2];
    memset(cmd, 0x00, sizeof(cmd));
    memcpy(cmd, recv_buffer, 2);

    char send_buffer[16];
    memset(send_buffer, 0x00, sizeof(send_buffer));
    memcpy(send_buffer, cmd, 2);

    MFRC522::StatusCode status = MFRC522::STATUS_ERROR;

    // RFID 카드가 정상 인식된 경우
    if (newCard == true && readCard == true)
    {
      // GS가 아닌 경우 UID 확인
      if (strncmp(cmd, "GS", 2) != 0)
      {
        if (memcmp(recv_buffer + 2, rc522.uid.uidByte, 4) != 0)
        {
          memset(send_buffer + 2, 0xFB, 1);
          Serial.write(send_buffer, 3);
          Serial.println();
          return;
        }
      }

      // GS : 카드 UID 요청
      if (strncmp(cmd, "GS", 2) == 0)
      {
        memset(send_buffer + 2, MFRC522::STATUS_OK, 1);
        memcpy(send_buffer + 3, rc522.uid.uidByte, 4);
        Serial.write(send_buffer, 7);
      }

      // GT : 잔액 읽기
      else if (strncmp(cmd, "GT", 2) == 0)
      {
        byte total[4];
        memset(total, 0x00, 4);

        status = readData(TOTAL_INDEX, total);

        memset(send_buffer + 2, status, 1);
        memcpy(send_buffer + 3, total, 4);

        Serial.write(send_buffer, 7);
      }

      // ST : 잔액 변경
      else if (strncmp(cmd, "ST", 2) == 0)
      {
        char total[4];
        memset(total, 0x00, sizeof(total));

        memcpy(total, recv_buffer + 6, 4);

        status = writeData(TOTAL_INDEX, (byte*)total, 4);

        memset(send_buffer + 2, status, 1);
        Serial.write(send_buffer, 3);
      }

      // 알 수 없는 명령
      else
      {
        memset(send_buffer + 2, 0xFE, 1);
        Serial.write(send_buffer, 3);
      }

      rc522.PCD_StopCrypto1();
    }

    // 카드 없음
    else
    {
      memset(send_buffer + 2, 0xFA, 1);
      Serial.write(send_buffer, 3);
    }

    Serial.println();
  }

  // 슬롯 상태 확인
  checkSlots();
}
