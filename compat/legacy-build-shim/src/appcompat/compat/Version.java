package appcompat.compat;

/**
 * Guest-visible replacement for android.os.Build.VERSION.
 *
 * AppCompat translates references only inside imported legacy guest DEX files.
 * The compatibility engine itself continues to use the phone's real Build.VERSION.
 * Keep this class dependency-free so CI can compile it into a tiny standalone DEX.
 */
public final class Version {
    private Version() {}

    /** Android 7.1 / API 25: newest pre-Oreo framework identity. */
    public static final int SDK_INT = 25;
    public static final String SDK = "25";
    public static final String RELEASE = "7.1.1";
    public static final String CODENAME = "REL";
    public static final String INCREMENTAL = "appcompat-api25";
    public static final String BASE_OS = "";
    public static final String SECURITY_PATCH = "2017-10-05";
    public static final int PREVIEW_SDK_INT = 0;
    public static final int RESOURCES_SDK_INT = 25;
}
