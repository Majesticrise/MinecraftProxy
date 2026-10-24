package mcproxy;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.lang.instrument.Instrumentation;
import java.util.HashMap;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.ConcurrentHashMap;

import net.bytebuddy.agent.builder.AgentBuilder;
import net.bytebuddy.asm.Advice;
import net.bytebuddy.description.type.TypeDescription;
import net.bytebuddy.dynamic.ClassFileLocator;
import net.bytebuddy.dynamic.loading.ClassInjector;
import net.bytebuddy.pool.TypePool;
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
 * 类加载器说明（实测坑）：
 *   官方启动器的游戏类在系统类加载器里，agent 类天然可见；
 *   但 Fabric(Knot) / Forge 的游戏类在**隔离的类加载器**里——被插桩的
 *   netty 类在解析内联的 mcproxy.PipelineHook 调用时看不见 agent 类，
 *   会抛 NoClassDefFoundError 并打断游戏逻辑。因此插桩前先把注入侧的
 *   类（MirrorConfig/PipelineHook/MirrorInstaller/MirrorHandler/MirrorSession）
 *   动态定义进目标加载器（ClassInjector 反射 defineClass），这是 AMP
 *   类 javaagent 的标准做法。
 *
 * 配置（优先级从高到低，默认 127.0.0.1:25566）：
 *   -javaagent:mc-mirror-agent.jar=127.0.0.1:25566   （推荐，写在 JVM 参数里）
 *   -Dmcproxy.mirror=127.0.0.1:25566
 */
public final class MirrorAgent {

    /** 会随 PipelineHook 一起注入目标加载器的类（不得引用 agent 侧的 MirrorAgent/byte-buddy） */
    private static final String[] INJECT_CLASSES = {
            "mcproxy.MirrorConfig",
            "mcproxy.PipelineHook",
            "mcproxy.MirrorInstaller",
            "mcproxy.MirrorHandler",
            "mcproxy.MirrorSession",
    };

    private static final Set<ClassLoader> INJECTED =
            ConcurrentHashMap.newKeySet();

    private MirrorAgent() {
    }

    public static void premain(String agentArgs, Instrumentation inst) {
        if (agentArgs != null) {
            String trimmed = agentArgs.trim();
            if (!trimmed.isEmpty() && trimmed.indexOf(':') > 0) {
                // 系统属性全局可见：注入侧的 MirrorConfig 靠它拿地址
                System.setProperty("mcproxy.mirror.address", trimmed);
            }
        }
        log("启动，镜像目标 " + MirrorConfig.address());

        try {
            new AgentBuilder.Default()
                    .with(new SilentListener())
                    .type(ElementMatchers.named("io.netty.channel.DefaultChannelPipeline"))
                    .transform((builder, type, classLoader, module, protectionDomain) -> {
                        // 先保证注入侧类在目标加载器可见；失败则放弃插桩，
                        // 绝不让类解析错误打断游戏（实测 Fabric 上会炸连接）
                        if (!ensureInjected(classLoader)) {
                            return builder;
                        }
                        return builder
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
                                                .and(ElementMatchers.not(ElementMatchers.isStatic()))));
                    })
                    .installOn(inst);
            log("netty 插桩就绪（等待游戏连接服务器）");
        } catch (Throwable t) {
            log("初始化失败: " + t);
        }
    }

    /**
     * 确保注入侧类在目标加载器可见：
     * 已可见（官方启动器/系统加载器）直接放行；否则反射 defineClass 注入。
     */
    static boolean ensureInjected(ClassLoader loader) {
        if (loader == null) {
            return false; // bootstrap：netty 不该在这里
        }
        if (INJECTED.contains(loader)) {
            return true;
        }
        try {
            Class.forName("mcproxy.PipelineHook", false, loader);
            INJECTED.add(loader);
            return true; // 官方启动器：agent 与游戏同在系统加载器
        } catch (ClassNotFoundException expected) {
            // 需要注入，继续
        }
        try {
            ClassFileLocator locator = ClassFileLocator.ForClassLoader.of(
                    MirrorAgent.class.getClassLoader());
            TypePool pool = TypePool.Default.of(locator);
            Map<TypeDescription, byte[]> types = new HashMap<>();
            for (String name : INJECT_CLASSES) {
                types.put(pool.describe(name).resolve(), classBytes(name));
            }
            new ClassInjector.UsingReflection(loader).inject(types);
            INJECTED.add(loader);
            log("已把镜像类注入游戏类加载器 (" + loader.getClass().getName() + ")");
            return true;
        } catch (Throwable t) {
            log("类注入失败，跳过该加载器的插桩（游戏不受影响）: " + t);
            return false;
        }
    }

    private static byte[] classBytes(String name) throws Exception {
        String file = "/" + name.replace('.', '/') + ".class";
        try (InputStream in = MirrorAgent.class.getResourceAsStream(file)) {
            if (in == null) {
                throw new IllegalStateException("缺少类资源 " + file);
            }
            ByteArrayOutputStream out = new ByteArrayOutputStream();
            byte[] buffer = new byte[8192];
            int n;
            while ((n = in.read(buffer)) >= 0) {
                out.write(buffer, 0, n);
            }
            return out.toByteArray();
        }
    }

    static void log(String message) {
        System.out.println("[mc-mirror-agent] " + message);
    }

    /** 静默监听：只在插桩失败时留一条日志 */
    private static final class SilentListener implements AgentBuilder.Listener {
        @Override
        public void onDiscovery(String typeName, ClassLoader cl, JavaModule m, boolean loaded) {
        }

        @Override
        public void onTransformation(TypeDescription type, ClassLoader cl, JavaModule m,
                                     boolean loaded, net.bytebuddy.dynamic.DynamicType dt) {
            log("已插桩 " + type.getName());
        }

        @Override
        public void onIgnored(TypeDescription type, ClassLoader cl, JavaModule m, boolean loaded) {
        }

        @Override
        public void onError(String typeName, ClassLoader cl, JavaModule m,
                            boolean loaded, Throwable throwable) {
            log("插桩失败 " + typeName + ": " + throwable);
        }

        @Override
        public void onComplete(String typeName, ClassLoader cl, JavaModule m, boolean loaded) {
        }
    }
}
