#ifndef __SUBSYSTEM_HPP__
#define __SUBSYSTEM_HPP__

#include <ostream>
#include <string>
#include <memory>

class TCgroup;
class TMemorySubsystem;
class TFreezerSubsystem;
class TCpuSubsystem;

class TSubsystem {
    std::string name;

public:
    static std::shared_ptr<TSubsystem> Get(std::string name);

    static std::shared_ptr<TMemorySubsystem> Memory();
    static std::shared_ptr<TFreezerSubsystem> Freezer();
    static std::shared_ptr<TCpuSubsystem> Cpu();
    
    TSubsystem(std::string name);
    std::string Name();

    friend bool operator==(const TSubsystem& c1, const TSubsystem& c2) {
        return c1.name == c2.name;
    }

    friend std::ostream& operator<<(std::ostream& os, const TSubsystem& cg) {
        return (os << cg.name);
    }
};

class TMemorySubsystem : public TSubsystem {
public:
    TMemorySubsystem() : TSubsystem("memory") {}
};

class TFreezerSubsystem : public TSubsystem {
public:
    TFreezerSubsystem() : TSubsystem("freezer") {}

    void Freeze(TCgroup &cg);
    void Unfreeze(TCgroup &cg);
};

class TCpuSubsystem : public TSubsystem {
public:
    TCpuSubsystem() : TSubsystem("cpu") {}
};

#endif
