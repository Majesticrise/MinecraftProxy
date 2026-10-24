package mcproxy;

import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.net.Socket;
import java.util.concurrent.ArrayBlockingQueue;
import java.util.concurrent.BlockingQueue;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;

/**
 * 一条服务器连接对应的镜像通道。
 *
 * 由 netty 事件循环线程 offer（非阻塞，队满丢弃），独立 writer 线程
 * 负责连接 Python 侧并写出——任何失败都静默收尾，绝不影响游戏。
 *
 * 注意：本类会被注入到 Fabric(Knot) 等隔离类加载器，**不得使用匿名/
 * 内部类**（javac 生成的 $N 类不会随注入走，实测会 NoClassDefFoundError）。
 *
 * 线上协议（与 core/mirror.py 对应）：
 *   连接后先发 b"MCMR" + 版本号(1)
 *   之后每条消息 [方向 1B][帧长 4B 大端][明文裸帧]
 */
final class MirrorSession implements Runnable {

    static final int CLIENTBOUND = 0;   // 服务器 -> 客户端
    static final int SERVERBOUND = 1;   // 客户端 -> 服务器
    private static final byte[] MAGIC = {'M', 'C', 'M', 'R', 1};

    private static final int QUEUE_CAPACITY = 4096;
    private static final int CONNECT_TIMEOUT_MS = 3000;

    private final BlockingQueue<byte[]> queue = new ArrayBlockingQueue<byte[]>(QUEUE_CAPACITY);
    private final AtomicBoolean closed = new AtomicBoolean(false);
    private final Thread writer;

    MirrorSession() {
        writer = new Thread(this, "mcproxy-mirror-writer");
        writer.setDaemon(true);
        writer.start();
    }

    @Override
    public void run() {
        writeLoop();
    }

    void offer(int direction, byte[] bareFrame) {
        if (closed.get() || bareFrame == null || bareFrame.length == 0) {
            return;
        }
        int length = bareFrame.length;
        byte[] packet = new byte[5 + length];
        packet[0] = (byte) direction;
        packet[1] = (byte) (length >>> 24);
        packet[2] = (byte) (length >>> 16);
        packet[3] = (byte) (length >>> 8);
        packet[4] = (byte) length;
        System.arraycopy(bareFrame, 0, packet, 5, length);
        if (!queue.offer(packet)) {
            // 队列满（Python 侧未启动/卡住）：丢弃，绝不阻塞游戏线程
        }
    }

    void close() {
        if (!closed.compareAndSet(false, true)) {
            return;
        }
        writer.interrupt();
    }

    private void writeLoop() {
        Socket socket = null;
        try {
            String address = MirrorConfig.address();
            int colon = address.lastIndexOf(':');
            String host = colon > 0 ? address.substring(0, colon) : "127.0.0.1";
            int port = colon > 0 ? Integer.parseInt(address.substring(colon + 1)) : 25566;

            socket = new Socket();
            socket.connect(new InetSocketAddress(host, port), CONNECT_TIMEOUT_MS);
            socket.setTcpNoDelay(true);
            OutputStream out = socket.getOutputStream();
            out.write(MAGIC);
            out.flush();

            while (!closed.get()) {
                byte[] packet = queue.poll(200, TimeUnit.MILLISECONDS);
                if (packet == null) {
                    continue;
                }
                out.write(packet);
                if (queue.isEmpty()) {
                    out.flush();
                }
            }
            out.flush();
        } catch (Throwable ignored) {
            // Python 侧未启动、断开等：静默。游戏不受影响。
        } finally {
            if (socket != null) {
                try {
                    socket.close();
                } catch (Throwable ignored) {
                }
            }
            closed.set(true);
        }
    }
}
