package demo;
public class Controller {
    Api<String> api;
    public String execute() { return api.run("ok"); }
    public String direct() { return new Service().run("ok"); }
}
