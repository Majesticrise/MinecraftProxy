@echo off
rem Build mc-mirror-agent.jar (requires JDK 8+ on PATH)
setlocal
cd /d %~dp0

if not exist lib\byte-buddy.jar (
    echo [ERROR] lib\byte-buddy.jar or lib\netty-compile.jar not found. Download:
    echo   https://repo1.maven.org/maven2/net/bytebuddy/byte-buddy/1.17.5/byte-buddy-1.17.5.jar -^> lib\byte-buddy.jar
    echo   https://repo1.maven.org/maven2/io/netty/netty-all/4.1.68.Final/netty-all-4.1.68.Final.jar -^> lib\netty-compile.jar
    echo   netty is compile-only; the fat jar uses the client's bundled netty.
    exit /b 1
)

if exist build rmdir /s /q build
mkdir build

javac --release 8 -encoding UTF-8 -cp "lib\*" -d build src\mcproxy\*.java
if errorlevel 1 exit /b 1

rem Unpack byte-buddy into the fat jar, drop its manifest (ours wins)
pushd build
jar xf ..\lib\byte-buddy.jar
if exist META-INF\MANIFEST.MF del META-INF\MANIFEST.MF
popd

jar cfm mc-mirror-agent.jar MANIFEST.MF -C build .
if errorlevel 1 exit /b 1

echo Build OK: mc-mirror-agent.jar
echo Usage (launcher JVM args):
echo   -javaagent:^<full path^>\mc-mirror-agent.jar=127.0.0.1:25566
