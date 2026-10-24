import java.io.File;
import java.lang.reflect.Method;
import java.net.URL;
import java.net.URLClassLoader;

/**
 * 在**隔离的类加载器**里跑 TestHarness，模拟 Fabric(Knot)/Forge 环境：
 * netty 与 TestHarness 由子加载器加载，其父级是平台加载器（不含 agent 类），
 * agent 必须通过类注入才能让插桩代码解析 mcproxy.PipelineHook。
 *
 * 参数: <harnessClassesDir> <nettyJar>
 */
public class HarnessRunner {
    public static void main(String[] args) throws Exception {
        URL[] urls = {
                new File(args[0]).toURI().toURL(),
                new File(args[1]).toURI().toURL(),
        };
        // 父级为 null（只委派 bootstrap）：netty / mcproxy 类都无法走父级解析，
        // 只能来自子加载器 —— 与 Fabric Knot 的隔离等价
        try (URLClassLoader isolated = new URLClassLoader(urls, null)) {
            Class<?> main = Class.forName("TestHarness", true, isolated);
            Method method = main.getMethod("main", String[].class);
            method.invoke(null, (Object) new String[0]);
        }
    }
}
