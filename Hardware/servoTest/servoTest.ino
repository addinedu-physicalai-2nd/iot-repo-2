#include <ESP32Servo.h>
#include <Stepper.h>
#define servo1Pin 16
#define servo2Pin 17
#define servo3Pin 18
#define servoSlotPin 26

const int stepsPerRevolution = 2048;
Stepper myStepper(stepsPerRevolution, 23, 21, 22, 19);

Servo servos[4]; // 0,1,2 = 배출서보1~3, 3 = 슬롯서보

int currentAngle[4] = {0, 0, 0, 0}; // 서보별 현재 각도
int targetAngle[4] = {0, 0, 0, 0};  // 서보별 목표 각도
bool servoMoving[4] = {false, false, false, false};

unsigned long lastStepTime[4] = {0, 0, 0, 0};
const int servoStepDelay = 10; // 1도 이동 간격(ms) - 숫자 키우면 더 느려짐

String inputBuffer = "";

void setup()
{
  Serial.begin(115200);
  delay(500);

  ESP32PWM::allocateTimer(0);
  ESP32PWM::allocateTimer(1);
  ESP32PWM::allocateTimer(2);
  ESP32PWM::allocateTimer(3);

  servos[0].setPeriodHertz(50);
  servos[0].attach(servo1Pin, 500, 2400);

  servos[1].setPeriodHertz(50);
  servos[1].attach(servo2Pin, 500, 2400);

  servos[2].setPeriodHertz(50);
  servos[2].attach(servo3Pin, 500, 2400);

  servos[3].setPeriodHertz(50);
  servos[3].attach(servoSlotPin, 500, 2400);
  myStepper.setSpeed(14);

  for (int i = 0; i < 4; i++)
  {
    servos[i].write(0);
  }

  Serial.println("=== 서보 4개 개별 테스트 (천천히 이동) ===");
  Serial.println("형식: 서보번호 각도 (예: 1 50 -> 1번 서보를 50도로 천천히 이동)");
  Serial.println("서보번호: 1,2,3 = 배출서보1~3, 4 = 슬롯서보");
}

void loop()
{ 
  myStepper.step(20);
  while (Serial.available())
  {
    char c = Serial.read();
    if (c == '\n' || c == '\r')
    {
      if (inputBuffer.length() > 0)
      {
        int servoNum, angle;
        int parsed = sscanf(inputBuffer.c_str(), "%d %d", &servoNum, &angle);
        if (parsed == 2 && servoNum >= 1 && servoNum <= 4)
        {
          int idx = servoNum - 1;
          targetAngle[idx] = constrain(angle, 0, 180);
          servoMoving[idx] = true;
          Serial.print(">>> 서보 ");
          Serial.print(servoNum);
          Serial.print(" -> 목표각도 ");
          Serial.println(targetAngle[idx]);
        }
        else
        {
          Serial.println(">>> 형식 오류: '서보번호 각도' (예: 1 50)");
        }
        inputBuffer = "";
      }
    }
    else
    {
      inputBuffer += c;
    }
  }

  // 서보 4개를 각각 비차단으로 천천히 이동
  for (int i = 0; i < 4; i++)
  {
    if (servoMoving[i] && millis() - lastStepTime[i] >= servoStepDelay)
    {
      lastStepTime[i] = millis();
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