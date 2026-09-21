fn main() {
    cxx_build::bridge("src/lib.rs")
        .file("src/probe.cc")
        .include("include")
        .std("c++17")
        .compile("chirp-native-boundary-probe");
    println!("cargo:rerun-if-changed=src/lib.rs");
    println!("cargo:rerun-if-changed=src/probe.cc");
    println!("cargo:rerun-if-changed=include/probe.h");
}
