#!/usr/bin/env bash
# goodcare-uplink.jar 빌드: Uplink.java → classes(javac, android.jar) → classes.dex(d8) → jar
# 필요: ANDROID_HOME(플랫폼 android-31 이상, build-tools d8), JDK 17
set -euo pipefail
cd "$(dirname "$0")"
PLAT=$(ls -d "$ANDROID_HOME"/platforms/android-* | grep -E "/android-[0-9]+$" | sort -V | tail -1)  # 정식(베타 아님) 플랫폼
BT=$(ls -d "$ANDROID_HOME"/build-tools/* | grep -vE "rc|beta|preview" | sort -V | tail -1)
echo "platform=$PLAT build-tools=$BT"
rm -rf build && mkdir -p build/classes build/dex out
javac --release 11 -cp "$PLAT/android.jar" -d build/classes src/com/goodcare/a24/Uplink.java
"$BT/d8" --min-api 31 --lib "$PLAT/android.jar" --output build/dex $(find build/classes -name '*.class')
(cd build/dex && zip -q -X ../../out/goodcare-uplink.jar classes.dex)
SRC_SHA=$(sha256sum src/com/goodcare/a24/Uplink.java | cut -d' ' -f1)
JAR_SHA=$(sha256sum out/goodcare-uplink.jar | cut -d' ' -f1)
printf '{"protocol":"GCAR-1","sha256":"%s","source_sha256":"%s","platform":"%s"}\n' "$JAR_SHA" "$SRC_SHA" "$(basename "$PLAT")" > out/goodcare-uplink.json
cat out/goodcare-uplink.json
