package mcproxy;

import io.netty.buffer.ByteBuf;
import io.netty.channel.ChannelDuplexHandler;
import io.netty.channel.ChannelHandlerContext;
import io.netty.channel.ChannelPromise;

import java.io.ByteArrayOutputStream;
import java.util.zip.Inflater;

/**
 * 镜像抽头：原样放行经过的每一帧，同时复制一份发给 Python 侧。
 *
 * 两个实例（入站/出站）共用一个 MirrorSession（对应一条服务器连接）。
 * 关键约束：
 *   * 绝不修改、绝不吞掉消息——只是旁路复制，游戏行为零影响；
 *   * 复制用 getBytes（不动 readerIndex），随后立刻转发，无引用计数问题；
 *   * 镜像队列满时丢帧，绝不阻塞游戏线程。
 */
final class MirrorHandler extends ChannelDuplexHandler {

    private final MirrorSession session;
    /** true = 入站（服务器 -> 客户端）；false = 出站 */
    private final boolean inbound;

    MirrorHandler(MirrorSession session, boolean inbound) {
        this.session = session;
        this.inbound = inbound;
    }

    /** 重定位锚点时以此创建新实例（netty 不允许复用已 remove 的 handler 实例） */
    MirrorSession session() {
        return session;
    }

    @Override
    public void channelRead(ChannelHandlerContext ctx, Object msg) {
        if (inbound && msg instanceof ByteBuf) {
            try {
                // 入站抽头位于 decompressor（或 splitter）之后，恒为明文裸帧
                session.offer(MirrorSession.CLIENTBOUND, copy((ByteBuf) msg));
            } catch (Throwable ignored) {
            }
        }
        ctx.fireChannelRead(msg);
    }

    @Override
    public void write(ChannelHandlerContext ctx, Object msg, ChannelPromise promise) {
        if (!inbound && msg instanceof ByteBuf) {
            try {
                session.offer(MirrorSession.SERVERBOUND, toBareOutbound(ctx, (ByteBuf) msg));
            } catch (Throwable ignored) {
            }
        }
        ctx.write(msg, promise);
    }

    @Override
    public void channelInactive(ChannelHandlerContext ctx) throws Exception {
        if (inbound) {
            session.close();
        }
        super.channelInactive(ctx);
    }

    // ------------------------------------------------------------------ 工具

    private static byte[] copy(ByteBuf buf) {
        int length = buf.readableBytes();
        byte[] out = new byte[length];
        buf.getBytes(buf.readerIndex(), out);
        return out;
    }

    /**
     * 出站帧归一化为裸帧 [VarInt 包ID][载荷]。
     *
     * 未启用压缩时 prepender 前看到的就是 [id][payload]；
     * 启用压缩后是 [dataLen VarInt][dataLen==0 ? 明文 : zlib(id+payload)]。
     */
    private static byte[] toBareOutbound(ChannelHandlerContext ctx, ByteBuf buf) {
        byte[] data = copy(buf);
        if (ctx.pipeline().get("compressor") == null) {
            return data;
        }
        int[] pos = {0};
        long dataLen = readVarInt(data, pos);
        if (dataLen <= 0) {
            return slice(data, pos[0]);
        }
        return inflate(slice(data, pos[0]), (int) dataLen);
    }

    /** 读无符号 VarInt32（Minecraft 协议格式） */
    private static long readVarInt(byte[] data, int[] pos) {
        long value = 0;
        int shift = 0;
        while (pos[0] < data.length && shift < 35) {
            byte b = data[pos[0]++];
            value |= (long) (b & 0x7F) << shift;
            if ((b & 0x80) == 0) {
                return value;
            }
            shift += 7;
        }
        return -1;
    }

    private static byte[] slice(byte[] data, int from) {
        int length = data.length - from;
        if (length <= 0) {
            return new byte[0];
        }
        byte[] out = new byte[length];
        System.arraycopy(data, from, out, 0, length);
        return out;
    }

    private static byte[] inflate(byte[] input, int expectedSize) {
        Inflater inflater = new Inflater();
        try {
            inflater.setInput(input);
            int size = Math.max(expectedSize, 256);
            byte[] buffer = new byte[size];
            int n = inflater.inflate(buffer);
            if (inflater.finished()) {
                byte[] out = new byte[n];
                System.arraycopy(buffer, 0, out, 0, n);
                return out;
            }
            ByteArrayOutputStream bos = new ByteArrayOutputStream(n);
            bos.write(buffer, 0, n);
            byte[] chunk = new byte[4096];
            while (!inflater.finished()) {
                int m = inflater.inflate(chunk);
                bos.write(chunk, 0, m);
            }
            return bos.toByteArray();
        } catch (Exception e) {
            return input; // 解压失败时原样发（Python 侧会忽略畸形帧）
        } finally {
            inflater.end();
        }
    }
}
