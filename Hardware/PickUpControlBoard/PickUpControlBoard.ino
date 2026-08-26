#include <SPI.h>
#include <MFRC522.h>

// =====================================================
// 1. RFID 설정
// =====================================================

const int RST_PIN = 9;
const int SS_PIN  = 10;

MFRC522 rc522(SS_PIN, RST_PIN);

// RFID 카드에서 잔액을 저장할 블록
const int TOTAL_INDEX = 60;

MFRC522::MIFARE_Key key;


// =====================================================
// 2. 픽업존 IR 센서 설정
// =====================================================

const int SLOT_COUNT = 3;

const int sensorPins[SLOT_COUNT] = {
  A0,
  A1,
  A2
};

// Arduino UNO의 analogRead()는 0 ~ 1023
// 실제 센서값 확인 후 조정
int threshold = 500;

// 현재 슬롯 상태
bool slotOccupied[SLOT_COUNT] = {
  false,
  false,
  false
};


// =====================================================
// 3. RFID 인증
// =====================================================

MFRC522::StatusCode checkAuth(int index)
{
  MFRC522::StatusCode status =
    rc522.PCD_Authenticate(
      MFRC522::PICC_CMD_MF_AUTH_KEY_A,
      index,
      &key,
      &(rc522.uid)
    );

  return status;
}


// =====================================================
// 4. RFID 데이터 읽기
// =====================================================

MFRC522::StatusCode readData(int index, byte* data)
{
  // 카드 인증
  MFRC522::StatusCode status = checkAuth(index);

  if (status != MFRC522::STATUS_OK)
  {
    return status;
  }

  byte buffer[18];
  byte length = 18;

  status = rc522.MIFARE_Read(
    index,
    buffer,
    &length
  );

  if (status == MFRC522::STATUS_OK)
  {
    // 잔액은 4Byte 사용
    memcpy(data, buffer, 4);
  }

  return status;
}


// =====================================================
// 5. RFID 데이터 쓰기
// =====================================================

MFRC522::StatusCode writeData(
  int index,
  byte* data,
  int length
)
{
  // 카드 인증
  MFRC522::StatusCode status = checkAuth(index);

  if (status != MFRC522::STATUS_OK)
  {
    return status;
  }

  // MIFARE block은 16Byte
  byte buffer[16];

  // 먼저 전부 0으로 초기화
  memset(buffer, 0x00, sizeof(buffer));

  // 실제 데이터 복사
  memcpy(buffer, data, length);

  // 카드에 기록
  status = rc522.MIFARE_Write(
    index,
    buffer,
    16
  );

  return status;
}


// =====================================================
// 6. 카드 선택
// =====================================================

// 같은 카드가 계속 리더기에 있어도
// 다시 읽을 수 있도록 WakeupA 사용
bool selectCard()
{
  byte bufferATQA[2];
  byte bufferSize = sizeof(bufferATQA);

  MFRC522::StatusCode status =
    rc522.PICC_WakeupA(
      bufferATQA,
      &bufferSize
    );

  if (status != MFRC522::STATUS_OK &&
      status != MFRC522::STATUS_COLLISION)
  {
    return false;
  }

  // UID 읽기
  if (!rc522.PICC_ReadCardSerial())
  {
    return false;
  }

  return true;
}


// =====================================================
// 7. Arduino 연결 알림
//
// HI + boardId
//
// boardId = 1 → Pickup Arduino
// =====================================================

void sendHello()
{
  byte buf[3];

  buf[0] = 'H';
  buf[1] = 'I';
  buf[2] = 0x01;

  Serial.write(buf, 3);
  Serial.write('\n');
}


// =====================================================
// 8. 슬롯 상태 전송
//
// SL + slot + occupied
//
// 예:
// SL 01 01
// → 1번 슬롯 물건 있음
//
// SL 01 00
// → 1번 슬롯 비어있음
// =====================================================

void sendSlotState(
  int slotNumber,
  bool occupied
)
{
  byte buf[4];

  buf[0] = 'S';
  buf[1] = 'L';

  buf[2] = (byte)slotNumber;

  if (occupied)
  {
    buf[3] = 0x01;
  }
  else
  {
    buf[3] = 0x00;
  }

  Serial.write(buf, 4);
  Serial.write('\n');
}


// =====================================================
// 9. 픽업 슬롯 확인
// =====================================================

void checkSlots()
{
  for (int i = 0; i < SLOT_COUNT; i++)
  {
    int value =
      analogRead(sensorPins[i]);

    // 현재 센서 기준
    // 값이 작으면 물체 있음
    bool occupied =
      (value < threshold);

    // 이전 상태와 달라진 경우만 전송
    if (occupied != slotOccupied[i])
    {
      slotOccupied[i] = occupied;

      sendSlotState(
        i + 1,
        occupied
      );
    }
  }
}


// =====================================================
// 10. RFID 명령 처리
// =====================================================

void processRFIDCommand(
  char* recv_buffer,
  int recv_size
)
{
  // 최소 명령 2Byte 필요
  if (recv_size < 2)
  {
    return;
  }


  // ---------------------------------------------------
  // 명령 추출
  // ---------------------------------------------------

  char cmd[3];

  cmd[0] = recv_buffer[0];
  cmd[1] = recv_buffer[1];
  cmd[2] = '\0';


  // ---------------------------------------------------
  // 송신 버퍼
  // ---------------------------------------------------

  byte send_buffer[16];

  memset(
    send_buffer,
    0x00,
    sizeof(send_buffer)
  );

  send_buffer[0] = cmd[0];
  send_buffer[1] = cmd[1];


  // ---------------------------------------------------
  // 카드 확인
  // ---------------------------------------------------

  bool cardFound = selectCard();

  if (!cardFound)
  {
    // 0xFA = NO_TAG

    send_buffer[2] = 0xFA;

    Serial.write(
      send_buffer,
      3
    );

    Serial.write('\n');

    return;
  }


  // ---------------------------------------------------
  // GS가 아닌 경우
  // 요청 UID와 현재 카드 UID 확인
  // ---------------------------------------------------

  if (strncmp(cmd, "GS", 2) != 0)
  {
    // GT는 최소 6Byte
    // ST는 최소 10Byte

    if (recv_size < 6)
    {
      send_buffer[2] = 0xFE;

      Serial.write(
        send_buffer,
        3
      );

      Serial.write('\n');

      return;
    }


    if (memcmp(
          recv_buffer + 2,
          rc522.uid.uidByte,
          4
        ) != 0)
    {
      // 0xFB = INVALID_TAG

      send_buffer[2] = 0xFB;

      Serial.write(
        send_buffer,
        3
      );

      Serial.write('\n');

      return;
    }
  }


  // ===================================================
  // GS
  //
  // Get Status
  // 카드 UID 반환
  // ===================================================

  if (strncmp(cmd, "GS", 2) == 0)
  {
    send_buffer[2] =
      MFRC522::STATUS_OK;

    memcpy(
      send_buffer + 3,
      rc522.uid.uidByte,
      4
    );

    // GS + Status + UID
    // 2 + 1 + 4 = 7Byte

    Serial.write(
      send_buffer,
      7
    );
  }


  // ===================================================
  // GT
  //
  // Get Total
  // 현재 카드 잔액 읽기
  // ===================================================

  else if (strncmp(cmd, "GT", 2) == 0)
  {
    byte total[4];

    memset(
      total,
      0x00,
      sizeof(total)
    );


    MFRC522::StatusCode status =
      readData(
        TOTAL_INDEX,
        total
      );


    send_buffer[2] = status;


    if (status == MFRC522::STATUS_OK)
    {
      memcpy(
        send_buffer + 3,
        total,
        4
      );
    }


    // GT + Status + Total
    // 2 + 1 + 4 = 7Byte

    Serial.write(
      send_buffer,
      7
    );
  }


  // ===================================================
  // ST
  //
  // Set Total
  // 새로운 카드 잔액 저장
  //
  // 충전과 결제 모두 이 명령 사용
  // ===================================================

  else if (strncmp(cmd, "ST", 2) == 0)
  {
    // ST
    // + UID 4Byte
    // + Total 4Byte
    //
    // 최소 10Byte

    if (recv_size < 10)
    {
      send_buffer[2] = 0xFE;

      Serial.write(
        send_buffer,
        3
      );

      Serial.write('\n');

      return;
    }


    byte total[4];

    memset(
      total,
      0x00,
      sizeof(total)
    );


    // recv_buffer 구조
    //
    // [0][1]     ST
    // [2~5]      UID
    // [6~9]      Total

    memcpy(
      total,
      recv_buffer + 6,
      4
    );


    MFRC522::StatusCode status =
      writeData(
        TOTAL_INDEX,
        total,
        4
      );


    send_buffer[2] = status;


    // ST + Status
    // 3Byte

    Serial.write(
      send_buffer,
      3
    );
  }


  // ===================================================
  // 알 수 없는 명령
  // ===================================================

  else
  {
    // 0xFE = UNKNOWN_COMMAND

    send_buffer[2] = 0xFE;

    Serial.write(
      send_buffer,
      3
    );
  }


  Serial.write('\n');

  // 암호화 인증 종료
  rc522.PCD_StopCrypto1();
}


// =====================================================
// SETUP
// =====================================================

void setup()
{
  // ---------------------------------------------------
  // Serial
  // ---------------------------------------------------

  Serial.begin(9600);

  // readBytesUntil()이 너무 오래
  // 기다리는 것 방지
  Serial.setTimeout(50);


  // ---------------------------------------------------
  // RFID Key
  // FF FF FF FF FF FF
  // ---------------------------------------------------

  for (int i = 0; i < 6; i++)
  {
    key.keyByte[i] = 0xFF;
  }


  // ---------------------------------------------------
  // IR Sensor
  // ---------------------------------------------------

  for (int i = 0; i < SLOT_COUNT; i++)
  {
    pinMode(
      sensorPins[i],
      INPUT
    );
  }


  // ---------------------------------------------------
  // RFID
  // ---------------------------------------------------

  SPI.begin();

  rc522.PCD_Init();


  // PC Serial 연결 준비시간
  delay(1000);


  // ---------------------------------------------------
  // Pickup Arduino 연결 알림
  // ---------------------------------------------------

  sendHello();


  // 시작할 때 현재 슬롯 상태도
  // 한번 보내기
  for (int i = 0; i < SLOT_COUNT; i++)
  {
    int value =
      analogRead(sensorPins[i]);

    slotOccupied[i] =
      (value < threshold);

    sendSlotState(
      i + 1,
      slotOccupied[i]
    );
  }
}


// =====================================================
// LOOP
// =====================================================

void loop()
{
  // ---------------------------------------------------
  // 1. PC 명령 확인
  // ---------------------------------------------------

  if (Serial.available() > 0)
  {
    char recv_buffer[16];

    memset(
      recv_buffer,
      0x00,
      sizeof(recv_buffer)
    );


    int recv_size =
      Serial.readBytesUntil(
        '\n',
        recv_buffer,
        sizeof(recv_buffer)
      );


    if (recv_size > 0)
    {
      processRFIDCommand(
        recv_buffer,
        recv_size
      );
    }
  }


  // ---------------------------------------------------
  // 2. 픽업존 센서 확인
  // ---------------------------------------------------

  checkSlots();
}
