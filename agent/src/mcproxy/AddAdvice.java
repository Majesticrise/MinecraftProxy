package mcproxy;

import net.bytebuddy.asm.Advice;

/**
 * 内联到 DefaultChannelPipeline.add*() 系列方法出口的通知。
 *
 * vanilla 在登录阶段会动态插入压缩/加密 handler（例如
 * addBefore("decoder","decompressor",...)），镜像抽头的锚点需要随之
 * 重定位（见 MirrorInstaller.resync）。
 */
public final class AddAdvice {

    @Advice.OnMethodExit
    public static void exit(@Advice.This Object pipeline, @Advice.AllArguments Object[] args) {
        PipelineHook.onAdd(pipeline, args);
    }

    private AddAdvice() {
    }
}
