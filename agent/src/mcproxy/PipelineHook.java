package mcproxy;

import io.netty.channel.DefaultChannelPipeline;

/**
 * 插桩回调入口（被内联的 advice 调用）。
 *
 * 本类只在 netty 存在的路径上被加载，因此可以安全引用 netty 类型；
 * 所有回调都不允许抛异常——绝不能因为镜像而影响游戏。
 */
public final class PipelineHook {

    private PipelineHook() {
    }

    /** DefaultChannelPipeline.fireChannelActive() 出口：尝试安装镜像 handler */
    public static void onFireChannelActive(Object pipeline) {
        try {
            if (pipeline instanceof DefaultChannelPipeline) {
                MirrorInstaller.install((DefaultChannelPipeline) pipeline);
            }
        } catch (Throwable t) {
            MirrorConfig.log("channelActive 钩子失败: " + t);
        }
    }

    /** DefaultChannelPipeline.add*() 出口：关注的 handler 出现时重定位锚点 */
    public static void onAdd(Object pipeline, Object[] args) {
        try {
            if (!(pipeline instanceof DefaultChannelPipeline)) {
                return;
            }
            boolean relevant = false;
            for (Object arg : args) {
                if (arg instanceof String && MirrorInstaller.isWatchedName((String) arg)) {
                    relevant = true;
                    break;
                }
            }
            if (!relevant) {
                return;
            }
            MirrorInstaller.resync((DefaultChannelPipeline) pipeline);
        } catch (Throwable t) {
            MirrorConfig.log("add 钩子失败: " + t);
        }
    }
}
