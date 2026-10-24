import io.netty.bootstrap.Bootstrap;
import io.netty.buffer.Unpooled;
import io.netty.channel.Channel;
import io.netty.channel.ChannelHandlerContext;
import io.netty.channel.ChannelInboundHandlerAdapter;
import io.netty.channel.ChannelInitializer;
import io.netty.channel.ChannelOutboundHandlerAdapter;
import io.netty.channel.ChannelPromise;
import io.netty.channel.nio.NioEventLoopGroup;
import io.netty.channel.socket.SocketChannel;
import io.netty.channel.socket.nio.NioSocketChannel;

import java.net.ServerSocket;
import java.net.Socket;

/**
 * 无需 Minecraft 的 agent 链路测试桩：
 * 构造与 vanilla 同名（splitter/prepender/decoder/packet_handler）的 netty 管道，
 * 由 -javaagent 挂载的 mc-mirror-agent 自动插桩并镜像帧。
 *
 * 帧序列（run_smoke.py 会校验收到的内容与顺序）：
 *   1) 出站 writeAndFlush [0x05 'h' 'i']        -> 镜像方向 1
 *   2) 入站 fireChannelRead [0x07 'y' 'o']      -> 镜像方向 0
 *   3) addBefore("decoder","decompressor",...)  -> 触发锚点重定位
 *   4) 入站 fireChannelRead [0x0c 'z' 'z']      -> 仍镜像方向 0
 */
public class TestHarness {

    public static void main(String[] args) throws Exception {
        // 本地“游戏服务器”：只接受连接并丢弃数据
        final ServerSocket serverSocket = new ServerSocket(0);
        Thread serverThread = new Thread(new Runnable() {
            @Override
            public void run() {
                try {
                    Socket client = serverSocket.accept();
                    byte[] buf = new byte[4096];
                    while (client.getInputStream().read(buf) >= 0) {
                        // 丢弃
                    }
                } catch (Exception ignored) {
                }
            }
        });
        serverThread.setDaemon(true);
        serverThread.start();

        NioEventLoopGroup group = new NioEventLoopGroup(1);
        try {
            Channel ch = new Bootstrap()
                    .group(group)
                    .channel(NioSocketChannel.class)
                    .handler(new ChannelInitializer<SocketChannel>() {
                        @Override
                        protected void initChannel(SocketChannel socket) {
                            socket.pipeline().addLast("prepender", new PassThroughOut());
                            socket.pipeline().addLast("splitter", new PassThroughIn());
                            socket.pipeline().addLast("decoder", new PassThroughIn());
                            socket.pipeline().addLast("packet_handler", new Sink());
                        }
                    })
                    .connect("127.0.0.1", serverSocket.getLocalPort())
                    .sync()
                    .channel();

            Thread.sleep(800); // 给 agent 在 fireChannelActive 处安装抽头的时间

            // 1) 出站帧
            ch.writeAndFlush(Unpooled.wrappedBuffer(new byte[]{0x05, 'h', 'i'})).sync();
            Thread.sleep(400);

            // 2) 入站帧（从管道头部注入，等价于 socket 收到数据）
            ch.pipeline().fireChannelRead(Unpooled.wrappedBuffer(new byte[]{0x07, 'y', 'o'}));
            Thread.sleep(400);

            // 3) 模拟 vanilla 登录期动态插入压缩 handler -> 触发锚点重定位
            ch.pipeline().addBefore("decoder", "decompressor", new PassThroughIn());
            Thread.sleep(400);

            // 4) 重定位后再来一帧入站
            ch.pipeline().fireChannelRead(Unpooled.wrappedBuffer(new byte[]{0x0c, 'z', 'z'}));
            Thread.sleep(800);

            System.out.println("HARNESS:DONE");
            System.exit(0);
        } finally {
            group.shutdownGracefully();
        }
    }

    /** 占位：等价 vanilla 的 splitter / decompressor / decoder（透传 ByteBuf） */
    public static class PassThroughIn extends ChannelInboundHandlerAdapter {
        @Override
        public void channelRead(ChannelHandlerContext ctx, Object msg) {
            ctx.fireChannelRead(msg);
        }
    }

    /** 占位：等价 vanilla 的 prepender（透传出站字节） */
    public static class PassThroughOut extends ChannelOutboundHandlerAdapter {
        @Override
        public void write(ChannelHandlerContext ctx, Object msg, ChannelPromise promise) {
            ctx.write(msg, promise);
        }
    }

    /** 占位：等价 vanilla 的 packet_handler（终结点，吞掉一切） */
    public static class Sink extends ChannelInboundHandlerAdapter {
        @Override
        public void channelRead(ChannelHandlerContext ctx, Object msg) {
            // 丢弃
        }
    }
}
