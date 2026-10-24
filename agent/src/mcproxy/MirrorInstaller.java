package mcproxy;

import io.netty.channel.Channel;
import io.netty.channel.ChannelHandler;
import io.netty.channel.ChannelPipeline;
import io.netty.channel.DefaultChannelPipeline;
import io.netty.channel.socket.SocketChannel;

import java.util.List;

/**
 * 把镜像 handler 安装进 Minecraft 的 netty 管道。
 *
 * vanilla 客户端管道（字符串常量名，不受混淆影响）：
 *
 *   入站： decrypt -> splitter -> decompressor -> decoder -> packet_handler
 *          （decrypt 挂在 splitter 之前：addBefore("splitter","decrypt")，
 *            即长度前缀也在密文内；decompressor 挂在 decoder 之前）
 *   出站： ... -> encoder -> compressor -> encrypt -> prepender -> socket
 *          （encrypt 挂在 prepender 之前：addBefore("prepender","encrypt")）
 *
 * 抽头位置：
 *   入站 mcproxy_in —— 跟在 {splitter, decrypt, decompressor} 中**管道顺序
 *   最靠后**的那个之后。这里看到的是解密、解压全部完成后的明文裸帧
 *   [VarInt 包ID][载荷]（长度前缀已被 splitter 消费）。
 *   压缩/加密 handler 是登录阶段动态插入的，因此 add* 钩子负责重定位锚点。
 *
 *   出站 mcproxy_out —— 挂在 prepender 之前。出站方向的处理顺序是从管道
 *   尾向头，先到我们、后到 encrypt，因此同样永远是明文（可能是已压缩的
 *   [dataLen][zlib]，由 MirrorHandler 解压成裸帧）。
 */
public final class MirrorInstaller {

    static final String IN_NAME = "mcproxy_in";
    static final String OUT_NAME = "mcproxy_out";

    /** 入站锚点候选（取管道顺序最靠后者） */
    private static final String[] IN_ANCHORS = {"splitter", "decrypt", "decompressor"};
    /** 识别 Minecraft 连接的管道标记（任一存在即可） */
    private static final String[] MC_MARKERS = {"packet_handler", "splitter"};

    private MirrorInstaller() {
    }

    /** add* 钩子只关心这些名字（用于触发锚点重定位） */
    static boolean isWatchedName(String name) {
        for (String anchor : IN_ANCHORS) {
            if (anchor.equals(name)) {
                return true;
            }
        }
        return false;
    }

    static void install(DefaultChannelPipeline pipeline) {
        ChannelPipeline p = pipeline;
        if (p.get(IN_NAME) != null) {
            return; // 已安装
        }
        Channel channel = p.channel();
        if (!(channel instanceof SocketChannel)) {
            return; // 只处理 TCP 客户端连接
        }
        if (!isMinecraft(p)) {
            return; // 非 Minecraft 协议的 netty 连接（模组的 HTTP/语音等）
        }
        try {
            // 若 OUT 抽头还在（例如上次 resync 半途失败只丢了 IN），复用其 session，
            // 避免出现「入站走新连接、出站走旧连接」的分裂
            ChannelHandler existingOut = p.get(OUT_NAME);
            MirrorSession session = (existingOut instanceof MirrorHandler)
                    ? ((MirrorHandler) existingOut).session()
                    : new MirrorSession();
            if (existingOut == null && p.get("prepender") != null) {
                p.addBefore("prepender", OUT_NAME, new MirrorHandler(session, false));
            }
            String anchor = lastAnchor(p);
            if (anchor != null) {
                p.addAfter(anchor, IN_NAME, new MirrorHandler(session, true));
            } else {
                p.addFirst(IN_NAME, new MirrorHandler(session, true));
            }
            MirrorAgent.log("已附加镜像抽头 (" + channel.remoteAddress() + ")");
        } catch (Throwable t) {
            MirrorAgent.log("安装镜像抽头失败: " + t);
        }
    }

    /**
     * 管道发生 add* 之后：尚未安装则尝试安装（晚建立的连接），
     * 已安装则检查入站锚点是否需要后移（decompressor/decrypt 晚到）。
     */
    static void resync(DefaultChannelPipeline pipeline) {
        ChannelPipeline p = pipeline;
        ChannelHandler handler = p.get(IN_NAME);
        if (!(handler instanceof MirrorHandler)) {
            if (p.channel().isActive() && isMinecraft(p)) {
                install(pipeline);
            }
            return;
        }
        String desired = lastAnchor(p);
        if (desired == null) {
            return;
        }
        List<String> names = p.names();
        int inIndex = names.indexOf(IN_NAME);
        int desiredIndex = names.indexOf(desired);
        if (inIndex < 0 || desiredIndex < 0 || inIndex > desiredIndex) {
            return; // 已位于锚点之后
        }
        try {
            MirrorSession session = ((MirrorHandler) handler).session();
            p.remove(IN_NAME);
            // netty 不允许重复添加已 remove 的实例，必须换新 handler（共享 session）
            p.addAfter(desired, IN_NAME, new MirrorHandler(session, true));
            MirrorAgent.log("镜像抽头已重定位到 " + desired + " 之后");
        } catch (Throwable t) {
            MirrorAgent.log("重定位镜像抽头失败: " + t);
        }
    }

    private static boolean isMinecraft(ChannelPipeline p) {
        for (String marker : MC_MARKERS) {
            if (p.get(marker) != null) {
                return true;
            }
        }
        return false;
    }

    /** 返回 {splitter, decrypt, decompressor} 中管道顺序最靠后的名字 */
    private static String lastAnchor(ChannelPipeline p) {
        List<String> names = p.names();
        String found = null;
        int best = -1;
        for (String anchor : IN_ANCHORS) {
            int index = names.indexOf(anchor);
            if (index >= 0 && index > best) {
                best = index;
                found = anchor;
            }
        }
        return found;
    }
}
