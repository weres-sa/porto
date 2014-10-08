#include <sstream>

#include "util/string.hpp"

using std::string;
using std::vector;
using std::set;
using std::istringstream;

string CommaSeparatedList(const vector<string> &list) {
    string ret;
    for (auto c = list.begin(); c != list.end(); ) {
        ret += *c;
        if (++c != list.end())
            ret += ",";
    }
    return ret;
}

string CommaSeparatedList(const set<string> &list) {
    string ret;
    for (auto c = list.begin(); c != list.end(); ) {
        ret += *c;
        if (++c != list.end())
            ret += ",";
    }
    return ret;
}

TError StringsToIntegers(const std::vector<std::string> &strings,
                         std::vector<int> &integers) {
    for (auto l : strings) {
        try {
            integers.push_back(stoi(l));
        } catch (...) {
            return TError(EError::Unknown, string(__func__) + ": Bad integer value");
        }
    }

    return TError::Success();
}

TError StringToUint32(const std::string &str, uint32_t &value) {
    try {
        value = stoul(str);
    } catch (...) {
        return TError(EError::Unknown, string(__func__) + ": Bad integer value");
    }

    return TError::Success();
}

TError StringToUint64(const std::string &str, uint64_t &value) {
    try {
        value = stoull(str);
    } catch (...) {
        return TError(EError::Unknown, string(__func__) + ": Bad integer value");
    }

    return TError::Success();
}

TError StringToInt64(const std::string &str, int64_t &value) {
    try {
        value = stoll(str);
    } catch (...) {
        return TError(EError::Unknown, string(__func__) + ": Bad integer value");
    }

    return TError::Success();
}

TError StringToInt(const std::string &str, int &value) {
    try {
        value = stoi(str);
    } catch (...) {
        return TError(EError::Unknown, string(__func__) + ": Bad integer value");
    }

    return TError::Success();
}

TError StringWithUnitToUint64(const std::string &str, uint64_t &value) {
    try {
        size_t pos = 0;
        value = stoull(str, &pos);
        if (pos > 0 && pos < str.length()) {
            switch (str[pos]) {
            case 'G':
            case 'g':
                value <<= 10;
            case 'M':
            case 'm':
                value <<= 10;
            case 'K':
            case 'k':
                value <<= 10;
            default:
                break;
            }
        }
    } catch (...) {
        return TError(EError::Unknown, string(__func__) + ": Bad integer value");
    }

    return TError::Success();
}

TError SplitString(const std::string &s, const char sep, std::vector<std::string> &tokens) {
    try {
        istringstream ss(s);
        string tok;

        while(std::getline(ss, tok, sep))
            tokens.push_back(tok);
    } catch (...) {
        return TError(EError::Unknown, string(__func__) + ": Can't split string");
    }

    return TError::Success();
}

std::string StringTrim(const std::string& s)
{
    std::size_t first = s.find_first_not_of(' ');
    std::size_t last  = s.find_last_not_of(' ');
    return s.substr(first, last - first + 1);
}
