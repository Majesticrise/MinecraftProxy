package mcproxy;

import java.lang.instrument.Instrumentation;

import net.bytebuddy.agent.builder.AgentBuilder;
import net.bytebuddy.asm.Advice;
import net.bytebuddy.description.type.TypeDescription;
import net.bytebuddy.dynamic.DynamicType;
import net.bytebuddy.matcher.ElementMatchers;
import net.bytebuddy.utility.JavaModule;

/**
 * mc-mirror-agent —— Minecraft 客户端 javaagent（镜像模式）。
 *
 * 作用：在客户端进程内、netty 管道「解密之后、包解码之前」的位置，
 * 把明文数据帧抄送给 MinecraftProxy（python main.py --mirror-port N）。
 * 用于在正版验证（online-mode=true）服务器上抓取区块——加密由客户端
 * 原有 handler 完成，本 agent 不接触密钥、不修改任何字节，只旁路读取。
 *
 * 设计要点（为什么只插桩 netty）：
 *   * Minecraft 自己的类全是混淆名、每个版本都变；而 io.netty.* 包名稳定
 *     （客户端捆绑的 netty 4.1，十年未大变），插桩它才能跨版本免重编译。
 *   * vanilla 管道 handler 的名字是代码里的字符串常量，不受混淆影响：
 *     入站  decrypt -> splitter -> decompressor -> decoder -> packet_handler
 *     （decrypt 在 splitter 之前，即长度前缀也在密文内；见 MirrorInstaller）
 *
 * 配置（二选一，默认 127.0.0.1:25566）：
 *   -javaagent:mc-mirror-agent.jar=127.0.0.1:25566   （推荐，写在 JVM 参数里）
 *   -Dmcproxy.mirror=127.0.0.1:25566
 */
public final class MirrorAgent {

    /** -javaagent:jar=host:port 形式传入的地址（优先级最高） */
    private static volatile String agentArgAddress;

    private MirrorAgent() {
    }

    public static void premain(String agentArgs, Instrumentation inst) {
        if (agentArgs != null) {
            String trimmed = agentArgs.trim();
            if (!trimmed.isEmpty() && trimmed.indexOf(':') > 0) {
                agentArgAddress = trimmed;
            }
        }
        log("启动，镜像目标 " + address());

        try {
            new AgentBuilder.Default()
                    .with(new AgentBuilder.Listener() {
                        @Override
                        public void onDiscovery(
                                String typeName,
                                ClassLoader classLoader,
                                JavaModule module,
                                boolean loaded) {
                        }

                        @Override
                        public void onTransformation(
                                TypeDescription typeDescription,
                                ClassLoader classLoader,
                                JavaModule module,
                                boolean loaded,
                                DynamicType dynamicType) {
                            log("已插桩 " + typeDescription.getName());
                        }

                        @Override
                        public void onIgnored(
                                TypeDescription typeDescription,
                                ClassLoader classLoader,
                                JavaModule module,
                                boolean loaded) {
                        }

                        @Override
                        public void onError(
                                String typeName,
                                ClassLoader classLoader,
                                JavaModule module,
                                boolean loaded,
                                Throwable throwable) {
                            log("插桩失败 " + typeName + ": " + throwable);
                        }

                        @Override
                        public void onComplete(
                                String typeName,
                                ClassLoader classLoader,
                                JavaModule module,
                                boolean loaded) {
                        }
                    })
                    .type(ElementMatchers.named("io.netty.channel.DefaultChannelPipeline"))
                    .transform((builder, type, classLoader, module, protectionDomain) -> builder
                            // 连接激活：安装镜像 handler（此时 vanilla 已把
                            // prepender/splitter/decoder/packet_handler 装进管道）
                            .visit(Advice.to(ActiveAdvice.class).on(
                                    ElementMatchers.named("fireChannelActive")
                                            .and(ElementMatchers.takesArguments(0))))
                            // 管道变化：压缩器等晚到的 handler 需要重定位抽头锚点。
                            // 只匹配四个公开实例方法（addBefore0 等私有静态方法
                            // 没有 this，会让 @Advice.This 映射失败）
                            .visit(Advice.to(AddAdvice.class).on(
                                    ElementMatchers.named("addBefore")
                                            .or(ElementMatchers.named("addAfter"))
                                            .or(ElementMatchers.named("addFirst"))
                                            .or(ElementMatchers.named("addLast"))
                                            .and(ElementMatchers.not(ElementMatchers.isStatic())))))
                    .installOn(inst);
            log("netty 插桩就绪（等待游戏连接服务器）");
        } catch (Throwable t) {
            log("初始化失败: " + t);
        }
    }

    /** 镜像服务地址：-javaagent 参数 > -Dmcproxy.mirror > 默认值 */
    static String address() {
        if (agentArgAddress != null) {
            return agentArgAddress;
        }
        String prop = System.getProperty("mcproxy.mirror");
        if (prop != null && prop.trim().indexOf(':') > 0) {
            return prop.trim();
        }
        return "127.0.0.1:25566";
    }

    static void log(String message) {
        System.out.println("[mc-mirror-agent] " + message);
    }
}
