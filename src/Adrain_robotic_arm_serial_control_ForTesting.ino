#include <Servo.h>

// Update pins to match your robot arm wiring.
Servo baseServo;
Servo shoulderServo;
Servo elbowServo;
Servo gripperServo;

int basePos = 90;
int shoulderPos = 90;
int elbowPos = 90;
int gripperPos = 90;

const int STEP = 4;
const int SERIAL_BUF_LEN = 64;
char serialBuf[SERIAL_BUF_LEN];
uint8_t serialIdx = 0;

int clampInt(int value, int low, int high) {
  if (value < low) return low;
  if (value > high) return high;
  return value;
}

void applyPose() {
  baseServo.write(basePos);
  shoulderServo.write(shoulderPos);
  elbowServo.write(elbowPos);
  gripperServo.write(gripperPos);
}

bool applyCommand(char cmd) {
  switch (cmd) {
    case 'F': elbowPos += STEP; break;
    case 'N': break; // neutral: no movement
    default: return false;
  }

  basePos = clampInt(basePos, 0, 180);
  shoulderPos = clampInt(shoulderPos, 0, 180);
  elbowPos = clampInt(elbowPos, 0, 180);
  gripperPos = clampInt(gripperPos, 0, 180);
  applyPose();
  return true;
}

void sendAck(long seq, char cmd) {
  unsigned long nowUs = micros();
  Serial.print("ACK,");
  Serial.print(seq);
  Serial.print(",");
  Serial.print(cmd);
  Serial.print(",");
  Serial.println(nowUs);
}

void sendErr(long seq, const char* reason) {
  Serial.print("ERR,");
  Serial.print(seq);
  Serial.print(",");
  Serial.println(reason);
}

void processLine(char* line) {
  // Preferred format: CMD,<seq>,<cmd>
  // Backwards compatible format: <cmd>
  if (line[0] == '\0') return;

  if (strncmp(line, "CMD,", 4) != 0) {
    char legacyCmd = line[0];
    if (applyCommand(legacyCmd)) {
      sendAck(-1, legacyCmd);
    } else {
      sendErr(-1, "bad_legacy_cmd");
    }
    return;
  }

  char* token = strtok(line, ","); // CMD
  token = strtok(NULL, ",");       // seq
  if (token == NULL) {
    sendErr(-1, "missing_seq");
    return;
  }
  long seq = atol(token);

  token = strtok(NULL, ",");       // cmd
  if (token == NULL || token[0] == '\0') {
    sendErr(seq, "missing_cmd");
    return;
  }
  char cmd = token[0];

  if (!applyCommand(cmd)) {
    sendErr(seq, "unknown_cmd");
    return;
  }

  sendAck(seq, cmd);
}

void setup() {
  Serial.begin(115200);
  baseServo.attach(3);
  shoulderServo.attach(5);
  elbowServo.attach(6);
  gripperServo.attach(9);
  applyPose();
}

void loop() {
  while (Serial.available()) {
    char c = (char)Serial.read();
    if (c == '\r') continue;

    if (c == '\n') {
      serialBuf[serialIdx] = '\0';
      processLine(serialBuf);
      serialIdx = 0;
      continue;
    }

    if (serialIdx < (SERIAL_BUF_LEN - 1)) {
      serialBuf[serialIdx++] = c;
    } else {
      serialIdx = 0;
      sendErr(-1, "line_too_long");
    }
  }
}
