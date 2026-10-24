#!/usr/bin/env bash
# Build mc-mirror-agent.jar (requires JDK 8+ on PATH)
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -f lib/byte-buddy.jar ] || [ ! -f lib/netty-compile.jar ]; then
    echo "[ERROR] lib/byte-buddy.jar or lib/netty-compile.jar not found. Download:"
    echo "  https://repo1.maven.org/maven2/net/bytebuddy/byte-buddy/1.17.5/byte-buddy-1.17.5.jar -> lib/byte-buddy.jar"
    echo "  https://repo1.maven.org/maven2/io/netty/netty-all/4.1.68.Final/netty-all-4.1.68.Final.jar -> lib/netty-compile.jar"
    echo "  (netty 仅用于编译；运行时用客户端自带的 netty，不打进 fat jar)"
    exit 1
fi

rm -rf build
mkdir build

# lib/* 同时包含 byte-buddy（运行时打包）与 netty（仅编译期）
javac --release 8 -encoding UTF-8 -cp "lib/*" -d build src/mcproxy/*.java

# 把 byte-buddy 解进 fat jar，去掉它的 MANIFEST（用我们自己的）
( cd build && jar xf ../lib/byte-buddy.jar && rm -f META-INF/MANIFEST.MF )

jar cfm mc-mirror-agent.jar MANIFEST.MF -C build .

echo "Build OK: mc-mirror-agent.jar"
echo "Usage (launcher JVM args):"
echo "  -javaagent:$(pwd)/mc-mirror-agent.jar=127.0.0.1:25566"
