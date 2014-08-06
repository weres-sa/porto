#ifndef __FILE_HPP__
#define __FILE_HPP__

#include <string>
#include <vector>

#include "error.hpp"

class TFile {
    std::string path;

public:
    enum EFileType {
        Regular,
        Directory,
        Block,
        Character,
        Fifo,
        Link,
        Socket,
        Unknown,
        Any
    };    

    TFile(std::string path);

    std::string Path();
    EFileType Type();

    TError Remove();

    TError AsString(std::string &value);
    TError AsInt(int &value);
    TError AsLines(std::vector<std::string> &value);

    TError WriteStringNoAppend(std::string str);
    TError AppendString(std::string str);
};

#endif
