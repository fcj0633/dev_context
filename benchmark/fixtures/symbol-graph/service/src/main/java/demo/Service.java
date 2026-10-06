package demo;
public class Service implements Api<String> {
    public Service() {}
    public String run(String value) { return helper(value); }
    public String helper(String value) { return finish(value); }
    public String finish(String value) { return value; }
    public String helper(int value) { return Integer.toString(value); }
}
