import java.nio.file.*;
import java.security.*;
import java.util.*;
import org.apache.karaf.features.internal.model.processing.FeaturesProcessing;
import org.apache.karaf.features.LocationPattern;
import org.ops4j.pax.url.mvn.MavenResolver;
import org.ops4j.pax.url.mvn.MavenResolvers;
public class ActivationTest {
 public static void main(String[] args) throws Exception {
  Path job=Path.of(args[0]);Files.createDirectories(job);
  if(System.getProperty("jdk.net.hosts.file")==null)throw new AssertionError("private hosts-file resolver required");
  System.setSecurityManager(new SecurityManager(){
   public void checkPermission(Permission p){}
   public void checkConnect(String h,int p){if(p>=0)throw new AssertionError("NETWORK FORBIDDEN");}
   public void checkListen(int p){throw new AssertionError("LISTENER FORBIDDEN");}
  });
  String coordinates="org.openhab.addons.bundles/org.openhab.binding.androidtv/5.2.3.thermite1";
  String mvn="mvn:"+coordinates;
  var parsed=FeaturesProcessing.parseOverridesClauses(Set.of(mvn+";range=\"[5.2.3,5.2.4)\""));
  if(parsed.size()!=1)throw new AssertionError("mvn override rejected");
  var override=parsed.iterator().next();
  if(!new LocationPattern(override.getOriginalUri()).matches("mvn:org.openhab.addons.bundles/org.openhab.binding.androidtv/5.2.3"))throw new AssertionError("original not matched");
  System.out.println("PARSED "+override.getOriginalUri());
  Path repository=job.resolve("repository"), cache=job.resolve("cache");
  Path dest=repository.resolve("org/openhab/addons/bundles/org.openhab.binding.androidtv/5.2.3.thermite1/org.openhab.binding.androidtv-5.2.3.thermite1.jar");
  Files.createDirectories(dest.getParent());Files.copy(Path.of(args[1]),dest,StandardCopyOption.REPLACE_EXISTING);
  Path settings=job.resolve("settings.xml");Files.writeString(settings,"<settings xmlns=\"http://maven.apache.org/SETTINGS/1.0.0\"/>\n");
  Dictionary<String,String> props=new Hashtable<>();String pid="org.ops4j.pax.url.mvn";
  props.put(pid+".settings",settings.toString());props.put(pid+".localRepository",cache.toString());
  props.put(pid+".defaultRepositories",repository.toUri()+"@id=thermite.androidtv");
  props.put(pid+".repositories","");props.put(pid+".useFallbackRepositories","false");
  try(MavenResolver resolver=MavenResolvers.createMavenResolver(props,pid)){
   Path result=resolver.resolve(mvn).toPath();
   byte[] expected=MessageDigest.getInstance("SHA-256").digest(Files.readAllBytes(dest));
   if(!Arrays.equals(expected,MessageDigest.getInstance("SHA-256").digest(Files.readAllBytes(result))))throw new AssertionError("artifact mismatch");
   System.out.println("PASS actual Karaf4.4.11 parser+Pax2.7.1 private file defaultRepository resolution; network denied");
  }
 }
}
