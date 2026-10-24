package mcproxy;

import net.bytebuddy.asm.Advice;

/**
 * 内联到 DefaultChannelPipeline.fireChannelActive() 出口的通知。
 *
 * 注意：本类会被 ByteBuddy 读取并复制字节码，方法体必须保持极简——
 * 只做一次静态调用，不引用任何 netty 类型（netty 不存在时本类仍可加载）。
 */
public final class ActiveAdvice {

    @Advice.OnMethodExit
    public static void exit(@Advice.This Object pipeline) {
        PipelineHook.onFireChannelActive(pipeline);
    }

    private ActiveAdvice() {
    }
}
