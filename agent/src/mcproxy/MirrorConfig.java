package mcproxy;

/**
 * 注入侧配置（会被 ClassInjector 注入到游戏类加载器，与 agent 侧解耦）。
 *
 * 通信约定：agent 侧 premain 把最终镜像地址写入系统属性
 * mcproxy.mirror.address（系统属性全局可见，天然跨类加载器）。
 */
public final class MirrorConfig {

    private MirrorConfig() {
    }

    static String address() {
        String address = System.getProperty("mcproxy.mirror.address");
        if (address != null && address.trim().indexOf(':') > 0) {
            return address.trim();
        }
        String legacy = System.getProperty("mcproxy.mirror");
        if (legacy != null && legacy.trim().indexOf(':') > 0) {
            return legacy.trim();
        }
        return "127.0.0.1:25566";
    }

    static void log(String message) {
        System.out.println("[mc-mirror-agent] " + message);
    }
}
